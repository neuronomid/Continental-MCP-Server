"""Candidate extraction, versioned lifecycle, manual-only promotion, audit log.

Candidate IDs are immutable: a parameter change is a *new* candidate, never a
mutation of an existing one (mcp_phases.md Phase 7). The engine may only ever
write ``research_only``; every status change above that requires an explicit
human action — the gate evaluator recommends, a person promotes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select

from .. import FEE_MODEL_VERSION, REGIME_MODEL_VERSION
from ..db.models import CandidateAuditLog, PaperTrade, StrategyCandidate
from .gates import GateContext, GateResult, evaluate_gate


def make_candidate_id(
    label: str, price_bin_lo: float, price_bin_hi: float, scope: str,
    entry_style: str, direction: str,
) -> str:
    key = f"{label}|{price_bin_lo:.2f}|{price_bin_hi:.2f}|{scope}|{entry_style}|{direction}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    return f"cand-{label}-{scope.replace(':', '_')}-{entry_style}-{direction}-{digest}"


@dataclass
class CandidateEvidence:
    label: str
    price_bin_lo: float
    price_bin_hi: float
    scope: str
    entry_style: str
    direction: str
    n: int
    win_rate: float
    net_ev: float
    net_ev_ci_lower_95: float
    edge: float
    median_liquidity_usd: float | None = None
    fill_prob_maker: float | None = None
    fdr_pass: bool = True
    walk_forward_pass: bool = True
    regime: str | None = None
    filters: dict = field(default_factory=dict)

    @property
    def candidate_id(self) -> str:
        return make_candidate_id(
            self.label, self.price_bin_lo, self.price_bin_hi, self.scope,
            self.entry_style, self.direction,
        )


class NotPromotableError(RuntimeError):
    """Raised when a non-human actor attempts promotion above research_only."""


class CandidateRegistry:
    def __init__(self, session_factory, fee_model_version: str = FEE_MODEL_VERSION):
        self.session_factory = session_factory
        self.fee_model_version = fee_model_version

    # --- extraction --------------------------------------------------------
    def extract(self, evidences: list[CandidateEvidence]) -> list[str]:
        """Create/refresh research_only candidates. Never promotes."""
        created: list[str] = []
        with self.session_factory() as s:
            for ev in evidences:
                if not (ev.fdr_pass and ev.walk_forward_pass):
                    continue
                cid = ev.candidate_id
                cand = s.execute(
                    select(StrategyCandidate).where(StrategyCandidate.candidate_id == cid)
                ).scalar_one_or_none()
                is_new = cand is None
                if is_new:
                    cand = StrategyCandidate(candidate_id=cid, status="research_only", version=1)
                    s.add(cand)
                cand.label = ev.label
                cand.price_bin_lo = ev.price_bin_lo
                cand.price_bin_hi = ev.price_bin_hi
                cand.scope = ev.scope
                cand.entry_style = ev.entry_style
                cand.direction = ev.direction
                cand.regime = ev.regime
                cand.n = ev.n
                cand.win_rate = ev.win_rate
                cand.net_ev = ev.net_ev
                cand.net_ev_ci_lower_95 = ev.net_ev_ci_lower_95
                cand.edge = ev.edge
                cand.median_liquidity_usd = ev.median_liquidity_usd
                cand.fill_prob_maker = ev.fill_prob_maker
                cand.fdr_pass = ev.fdr_pass
                cand.walk_forward_pass = ev.walk_forward_pass
                cand.fee_model_version = self.fee_model_version
                cand.regime_model_version = REGIME_MODEL_VERSION
                cand.filters_json = ev.filters
                cand.evidence_json = {
                    "n": ev.n, "win_rate": ev.win_rate, "net_ev": ev.net_ev,
                    "net_ev_ci_lower_95": ev.net_ev_ci_lower_95, "edge": ev.edge,
                }
                s.flush()
                if is_new:
                    s.add(CandidateAuditLog(
                        candidate_id=cid, from_status=None, to_status="research_only",
                        reason="auto-extracted (FDR + walk-forward pass)", actor="engine",
                        details_json=cand.evidence_json,
                    ))
                    created.append(cid)
            s.commit()
        return created

    # --- gate evaluation (recommendation only) ----------------------------
    def evaluate(self, candidate_id: str, target_status: str, ctx: GateContext) -> GateResult:
        return evaluate_gate(target_status, ctx)

    # --- promotion (manual) -----------------------------------------------
    def set_status(
        self, candidate_id: str, to_status: str, actor: str, reason: str,
        details: dict | None = None,
    ) -> None:
        """Change a candidate's status.

        Only ``actor='human'`` may move a candidate above ``research_only``
        (except automated ``disabled`` which is always allowed as a safety valve).
        """
        above_research = to_status not in ("research_only", "disabled")
        if above_research and actor != "human":
            raise NotPromotableError(
                f"actor '{actor}' cannot promote to '{to_status}' — manual action required"
            )
        with self.session_factory() as s:
            cand = s.execute(
                select(StrategyCandidate).where(StrategyCandidate.candidate_id == candidate_id)
            ).scalar_one()
            from_status = cand.status
            cand.status = to_status
            s.add(CandidateAuditLog(
                candidate_id=candidate_id, from_status=from_status, to_status=to_status,
                reason=reason, actor=actor, details_json=details,
            ))
            s.commit()

    def promote(self, candidate_id: str, to_status: str, reason: str, ctx: GateContext,
                actor: str = "human") -> GateResult:
        """Evaluate the gate then promote iff it passes (and actor is human)."""
        result = evaluate_gate(to_status, ctx)
        if result.passed:
            self.set_status(candidate_id, to_status, actor=actor, reason=reason,
                            details={"checks": result.checks})
        return result

    def disable(self, candidate_id: str, reason: str, actor: str = "engine") -> None:
        self.set_status(candidate_id, "disabled", actor=actor, reason=reason)

    # --- champion / challenger --------------------------------------------
    def rank_by_paper_pnl(self, statuses=("champion", "challenger")) -> list[tuple[str, float]]:
        """Rank candidates in the given statuses by realised paper net PnL."""
        with self.session_factory() as s:
            cands = s.execute(
                select(StrategyCandidate).where(StrategyCandidate.status.in_(statuses))
            ).scalars().all()
            ranked = []
            for c in cands:
                trades = s.execute(
                    select(PaperTrade).where(PaperTrade.candidate_id == c.candidate_id)
                ).scalars().all()
                pnl = sum(t.pnl or 0.0 for t in trades)
                ranked.append((c.candidate_id, pnl))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def audit_trail(self, candidate_id: str) -> list[CandidateAuditLog]:
        with self.session_factory() as s:
            return list(s.execute(
                select(CandidateAuditLog)
                .where(CandidateAuditLog.candidate_id == candidate_id)
                .order_by(CandidateAuditLog.at.asc())
            ).scalars().all())
