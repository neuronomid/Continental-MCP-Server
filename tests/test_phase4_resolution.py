"""Phase 4 — resolution parsing, close-call, tie rule, snapshot back-labeling."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from pmre.collectors.resolution import (
    ResolutionService,
    classify_close_call,
    expected_outcome_from_proxy,
    parse_resolution,
)
from pmre.db.models import Market, MarketResolution, MarketToken, Snapshot
from tests.helpers import load_json_fixture


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.UTC)


# --- parsing --------------------------------------------------------------
def test_parse_resolution_up_win():
    r = parse_resolution(load_json_fixture("raw/gamma_resolved_up.json"))
    assert r.resolved is True
    assert r.winning_outcome == "UP"


def test_parse_resolution_down_win():
    r = parse_resolution(load_json_fixture("raw/gamma_resolved_down.json"))
    assert r.resolved is True
    assert r.winning_outcome == "DOWN"


def test_parse_resolution_not_yet_resolved():
    raw = {"outcomes": "[\"Up\",\"Down\"]", "outcomePrices": "[\"0.55\",\"0.45\"]", "closed": False}
    r = parse_resolution(raw)
    assert r.resolved is False
    assert r.winning_outcome is None


# --- tie rule + close call ------------------------------------------------
def test_tie_rule_end_ge_start_is_up():
    assert expected_outcome_from_proxy(100.0, 100.0) == "UP"  # exact tie → UP
    assert expected_outcome_from_proxy(100.01, 100.0) == "UP"
    assert expected_outcome_from_proxy(99.99, 100.0) == "DOWN"


def test_close_call_classifier_threshold():
    ptb = 108000.0
    # 1 bp above → within 2bp threshold → close call
    proxy_1bp = ptb * (1 + 1e-4)
    margin, close, tie = classify_close_call(proxy_1bp, ptb, threshold_bps=2.0)
    assert close is True
    assert not tie
    assert abs(margin - 1.0) < 1e-6
    # 5 bp above → not a close call
    proxy_5bp = ptb * (1 + 5e-4)
    _, close5, _ = classify_close_call(proxy_5bp, ptb, threshold_bps=2.0)
    assert close5 is False


def test_close_call_exact_tie_sets_tie_rule():
    margin, close, tie = classify_close_call(100.0, 100.0)
    assert margin == 0.0
    assert close is True
    assert tie is True


# --- service: resolve + back-label ----------------------------------------
def _seed_market_with_snapshots(db, winner_bias="UP"):
    with db.session() as s:
        m = Market(slug="btc-updown-5m-1783447200", price_to_beat=108000.0,
                   expected_resolution_time_utc=utc(2026, 7, 7, 18, 5))
        s.add(m)
        s.flush()
        s.add_all([
            MarketToken(market_id=m.id, token_id="U", outcome="UP", outcome_index=0),
            MarketToken(market_id=m.id, token_id="D", outcome="DOWN", outcome_index=1),
        ])
        # UP-favored snapshot (up mid 0.62 > down mid 0.40)
        s.add(Snapshot(
            market_id=m.id, label="t_240", target_seconds_left=240,
            captured_at=utc(2026, 7, 7, 18, 1),
            up_mid=0.62, down_mid=0.40, up_best_ask=0.63, down_best_ask=0.41,
            dominant_side="UP", last_trade_price=0.62,
        ))
        # DOWN-favored snapshot
        s.add(Snapshot(
            market_id=m.id, label="t_30", target_seconds_left=30,
            captured_at=utc(2026, 7, 7, 18, 4, 30),
            up_mid=0.35, down_mid=0.66, up_best_ask=0.37, down_best_ask=0.67,
            dominant_side="DOWN", last_trade_price=0.66,
        ))
        mid = m.id
    return mid


def test_resolve_up_labels_tokens_and_snapshots(db):
    mid = _seed_market_with_snapshots(db)
    svc = ResolutionService(db.session_factory)
    parsed = parse_resolution(load_json_fixture("raw/gamma_resolved_up.json"))
    proxy_end = 108000.0 * (1 + 3e-4)  # 3 bps up, not a close call
    svc.resolve(mid, parsed, proxy_end=proxy_end, price_to_beat=108000.0)

    with db.session() as s:
        res = s.execute(select(MarketResolution).where(MarketResolution.market_id == mid)).scalar_one()
        assert res.winning_outcome == "UP"
        assert res.was_close_call is False
        up = s.execute(select(MarketToken).where(MarketToken.market_id == mid, MarketToken.outcome == "UP")).scalar_one()
        dn = s.execute(select(MarketToken).where(MarketToken.market_id == mid, MarketToken.outcome == "DOWN")).scalar_one()
        assert up.is_winner is True and dn.is_winner is False

        snaps = {sn.label: sn for sn in s.execute(select(Snapshot).where(Snapshot.market_id == mid)).scalars()}
        # UP won: t_240 (UP-favored) correct; t_30 (DOWN-favored) incorrect
        assert snaps["t_240"].was_correct_mid is True
        assert snaps["t_240"].was_correct_ask is True
        assert snaps["t_240"].was_correct_last_trade is True
        assert snaps["t_30"].was_correct_mid is False
        assert snaps["t_30"].was_correct_last_trade is False


def test_resolve_down_flips_correctness(db):
    mid = _seed_market_with_snapshots(db)
    svc = ResolutionService(db.session_factory)
    parsed = parse_resolution(load_json_fixture("raw/gamma_resolved_down.json"))
    # DOWN win: proxy below ptb
    svc.resolve(mid, parsed, proxy_end=108000.0 * (1 - 3e-4), price_to_beat=108000.0)
    with db.session() as s:
        snaps = {sn.label: sn for sn in s.execute(select(Snapshot).where(Snapshot.market_id == mid)).scalars()}
        assert snaps["t_240"].was_correct_mid is False  # UP-favored, DOWN won
        assert snaps["t_30"].was_correct_mid is True    # DOWN-favored, DOWN won


def test_resolve_close_call_flagged_on_snapshots(db):
    mid = _seed_market_with_snapshots(db)
    svc = ResolutionService(db.session_factory)
    parsed = parse_resolution(load_json_fixture("raw/gamma_resolved_up.json"))
    # 0.5 bp above ptb → close call
    svc.resolve(mid, parsed, proxy_end=108000.0 * (1 + 0.5e-4), price_to_beat=108000.0)
    with db.session() as s:
        for sn in s.execute(select(Snapshot).where(Snapshot.market_id == mid)).scalars():
            assert sn.was_close_call is True
        res = s.execute(select(MarketResolution).where(MarketResolution.market_id == mid)).scalar_one()
        assert res.was_close_call is True


def test_unresolved_overdue_alerts(db):
    from pmre.ops.health import HealthMonitor

    with db.session() as s:
        s.add(Market(slug="btc-updown-5m-old", expected_resolution_time_utc=utc(2026, 7, 7, 12, 0)))
        s.commit()
    hm = HealthMonitor(db.session_factory)
    svc = ResolutionService(db.session_factory, health=hm)
    overdue = svc.unresolved_overdue(now=utc(2026, 7, 7, 18, 0), grace_minutes=10)
    assert len(overdue) == 1


def test_resolve_rejects_unresolved():
    from pmre.collectors.resolution import ParsedResolution

    svc = ResolutionService(lambda: None)
    with pytest.raises(ValueError):
        svc.resolve(1, ParsedResolution(resolved=False, winning_outcome=None, resolved_at=None))
