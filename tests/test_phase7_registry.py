"""Phase 7 — gate boundaries, no-auto-promotion, audit, end-to-end lifecycle."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pmre.db.models import PaperTrade, StrategyCandidate
from pmre.registry.candidates import (
    CandidateEvidence,
    CandidateRegistry,
    NotPromotableError,
    make_candidate_id,
)
from pmre.registry.gates import GateContext, evaluate_gate


def _good_evidence(**over):
    base = dict(
        label="t_240", price_bin_lo=0.60, price_bin_hi=0.62, scope="session:new_york",
        entry_style="taker_ask", direction="dominant", n=800, win_rate=0.67,
        net_ev=0.02, net_ev_ci_lower_95=0.008, edge=0.05, median_liquidity_usd=12.0,
        fdr_pass=True, walk_forward_pass=True,
    )
    base.update(over)
    return CandidateEvidence(**base)


# --- candidate id immutability -------------------------------------------
def test_candidate_id_deterministic_and_param_sensitive():
    a = make_candidate_id("t_240", 0.60, 0.62, "total", "taker_ask", "dominant")
    b = make_candidate_id("t_240", 0.60, 0.62, "total", "taker_ask", "dominant")
    c = make_candidate_id("t_240", 0.62, 0.64, "total", "taker_ask", "dominant")
    assert a == b
    assert a != c  # different bin → different candidate


# --- gate boundary table-driven ------------------------------------------
def test_gate_paper_only_boundaries():
    ctx = GateContext(n=500, fdr_pass=True, walk_forward_pass=True,
                      net_ev_ci_lower_95=0.001, median_liquidity_usd=5.0,
                      planned_clip_size_usd=5.0)
    assert evaluate_gate("paper_only", ctx).passed is True
    # n just below floor fails
    assert evaluate_gate("paper_only", GateContext(**{**ctx.__dict__, "n": 499})).passed is False
    # ci lower exactly 0 fails (strict > 0)
    assert evaluate_gate("paper_only", GateContext(**{**ctx.__dict__, "net_ev_ci_lower_95": 0.0})).passed is False
    # liquidity below clip fails
    assert evaluate_gate("paper_only", GateContext(**{**ctx.__dict__, "median_liquidity_usd": 4.99})).passed is False
    # missing FDR fails
    assert evaluate_gate("paper_only", GateContext(**{**ctx.__dict__, "fdr_pass": False})).passed is False


def test_gate_challenger_boundaries():
    ctx = GateContext(paper_trade_count=200, paper_net_pnl=0.01,
                      realized_vs_model_ratio=0.20, monotonic_decay=False)
    assert evaluate_gate("challenger", ctx).passed is True
    assert evaluate_gate("challenger", GateContext(**{**ctx.__dict__, "paper_trade_count": 199})).passed is False
    assert evaluate_gate("challenger", GateContext(**{**ctx.__dict__, "realized_vs_model_ratio": 0.21})).passed is False
    assert evaluate_gate("challenger", GateContext(**{**ctx.__dict__, "monotonic_decay": True})).passed is False


def test_gate_disabled_or_semantics():
    assert evaluate_gate("disabled", GateContext(trailing_14d_ci_lower=-0.001)).passed is True
    assert evaluate_gate("disabled", GateContext(data_quality_regression=True)).passed is True
    assert evaluate_gate("disabled", GateContext(manual_disable=True)).passed is True
    assert evaluate_gate("disabled", GateContext()).passed is False


def test_gate_reasons_lists_failures():
    r = evaluate_gate("paper_only", GateContext(n=100, fdr_pass=False))
    assert "n_ge_500" in r.reasons()
    assert "fdr_pass" in r.reasons()


# --- extraction only ever writes research_only ---------------------------
def test_extract_creates_research_only(db):
    reg = CandidateRegistry(db.session_factory)
    created = reg.extract([_good_evidence()])
    assert len(created) == 1
    with db.session() as s:
        cand = s.execute(select(StrategyCandidate)).scalar_one()
        assert cand.status == "research_only"
        assert cand.scope == "session:new_york"
        assert cand.net_ev_ci_lower_95 == 0.008


def test_extract_skips_non_fdr_or_walkforward(db):
    reg = CandidateRegistry(db.session_factory)
    created = reg.extract([
        _good_evidence(fdr_pass=False),
        _good_evidence(price_bin_lo=0.64, price_bin_hi=0.66, walk_forward_pass=False),
    ])
    assert created == []


def test_extract_idempotent_refresh(db):
    reg = CandidateRegistry(db.session_factory)
    reg.extract([_good_evidence()])
    reg.extract([_good_evidence(win_rate=0.70)])  # refresh
    with db.session() as s:
        cands = s.execute(select(StrategyCandidate)).scalars().all()
        assert len(cands) == 1
        assert cands[0].win_rate == 0.70


# --- no auto-promotion ----------------------------------------------------
def test_engine_cannot_promote_above_research_only(db):
    reg = CandidateRegistry(db.session_factory)
    cid = reg.extract([_good_evidence()])[0]
    with pytest.raises(NotPromotableError):
        reg.set_status(cid, "paper_only", actor="engine", reason="nope")
    # still research_only
    with db.session() as s:
        assert s.execute(select(StrategyCandidate)).scalar_one().status == "research_only"


def test_promote_requires_passing_gate_and_human(db):
    reg = CandidateRegistry(db.session_factory)
    cid = reg.extract([_good_evidence()])[0]
    # failing gate → no promotion
    fail = reg.promote(cid, "paper_only", "try", GateContext(n=100), actor="human")
    assert fail.passed is False
    with db.session() as s:
        assert s.execute(select(StrategyCandidate)).scalar_one().status == "research_only"
    # passing gate + human → promoted
    ctx = GateContext(n=800, fdr_pass=True, walk_forward_pass=True,
                      net_ev_ci_lower_95=0.008, median_liquidity_usd=12.0, planned_clip_size_usd=5.0)
    ok = reg.promote(cid, "paper_only", "gates pass", ctx, actor="human")
    assert ok.passed is True
    with db.session() as s:
        assert s.execute(select(StrategyCandidate)).scalar_one().status == "paper_only"


# --- end-to-end lifecycle + audit ----------------------------------------
def test_end_to_end_lifecycle_and_audit(db):
    reg = CandidateRegistry(db.session_factory)
    cid = reg.extract([_good_evidence()])[0]
    ctx = GateContext(n=800, fdr_pass=True, walk_forward_pass=True,
                      net_ev_ci_lower_95=0.008, median_liquidity_usd=12.0, planned_clip_size_usd=5.0)
    reg.promote(cid, "paper_only", "promote to paper", ctx, actor="human")
    reg.disable(cid, "ci-lower negative over trailing 14d", actor="engine")

    with db.session() as s:
        cand = s.execute(select(StrategyCandidate)).scalar_one()
        assert cand.status == "disabled"
    trail = reg.audit_trail(cid)
    statuses = [(a.from_status, a.to_status) for a in trail]
    assert statuses == [(None, "research_only"), ("research_only", "paper_only"), ("paper_only", "disabled")]
    # disabled sticks: engine cannot re-promote
    with pytest.raises(NotPromotableError):
        reg.set_status(cid, "champion", actor="engine", reason="no")


def test_champion_challenger_ranking(db):
    reg = CandidateRegistry(db.session_factory)
    with db.session() as s:
        s.add(StrategyCandidate(candidate_id="c1", status="champion", label="t_240", entry_style="taker_ask"))
        s.add(StrategyCandidate(candidate_id="c2", status="challenger", label="t_240", entry_style="taker_ask"))
        s.flush()
        s.add_all([
            PaperTrade(client_uuid="u1", candidate_id="c1", pnl=1.0),
            PaperTrade(client_uuid="u2", candidate_id="c1", pnl=-0.5),
            PaperTrade(client_uuid="u3", candidate_id="c2", pnl=2.0),
        ])
        s.commit()
    ranked = reg.rank_by_paper_pnl()
    assert ranked[0] == ("c2", 2.0)
    assert ranked[1] == ("c1", 0.5)
