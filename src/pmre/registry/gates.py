"""Promotion-gate evaluator (mcp_plan.md §6.4).

The engine only ever *recommends*; promotion above ``research_only`` is a manual
act (enforced in :mod:`candidates`). These functions compute pass/fail with a
per-check breakdown so the evidence is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_STATUSES = (
    "research_only",
    "paper_only",
    "challenger",
    "champion",
    "tiny_live_allowed",
    "disabled",
)


@dataclass
class GateContext:
    # research_only → paper_only
    n: int = 0
    fdr_pass: bool = False
    walk_forward_pass: bool = False
    net_ev_ci_lower_95: float | None = None
    median_liquidity_usd: float | None = None
    planned_clip_size_usd: float = 5.0
    # paper_only → challenger/champion
    paper_trade_count: int = 0
    paper_net_pnl: float | None = None
    realized_vs_model_ratio: float | None = None  # |realized-model|/model
    monotonic_decay: bool = False
    # champion → tiny_live_allowed
    paper_days: int = 0
    human_reviewed: bool = False
    risk_caps_configured: bool = False
    # any → disabled
    trailing_14d_ci_lower: float | None = None
    data_quality_regression: bool = False
    manual_disable: bool = False


@dataclass
class GateResult:
    target_status: str
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)

    def reasons(self) -> list[str]:
        return [name for name, ok in self.checks.items() if not ok]


def _gate_paper_only(ctx: GateContext) -> dict[str, bool]:
    return {
        "n_ge_500": ctx.n >= 500,
        "fdr_pass": bool(ctx.fdr_pass),
        "walk_forward_pass": bool(ctx.walk_forward_pass),
        "ci_lower_net_ev_positive": (ctx.net_ev_ci_lower_95 or -1.0) > 0,
        "liquidity_ge_clip": (ctx.median_liquidity_usd or 0.0) >= ctx.planned_clip_size_usd,
    }


def _gate_challenger_or_champion(ctx: GateContext) -> dict[str, bool]:
    return {
        "paper_trades_ge_200": ctx.paper_trade_count >= 200,
        "paper_pnl_positive": (ctx.paper_net_pnl or -1.0) > 0,
        "realized_within_20pct": (ctx.realized_vs_model_ratio if ctx.realized_vs_model_ratio is not None else 1.0) <= 0.20,
        "no_monotonic_decay": not ctx.monotonic_decay,
    }


def _gate_tiny_live(ctx: GateContext) -> dict[str, bool]:
    return {
        "paper_days_ge_30": ctx.paper_days >= 30,
        "human_reviewed": ctx.human_reviewed,
        "risk_caps_configured": ctx.risk_caps_configured,
    }


def _gate_disabled(ctx: GateContext) -> dict[str, bool]:
    # 'disabled' triggers on ANY of these (OR semantics).
    return {
        "ci_lower_negative_14d": (ctx.trailing_14d_ci_lower is not None and ctx.trailing_14d_ci_lower < 0),
        "data_quality_regression": ctx.data_quality_regression,
        "manual": ctx.manual_disable,
    }


def evaluate_gate(target_status: str, ctx: GateContext) -> GateResult:
    if target_status not in VALID_STATUSES:
        raise ValueError(f"unknown target status: {target_status}")

    if target_status == "paper_only":
        checks = _gate_paper_only(ctx)
        passed = all(checks.values())
    elif target_status in ("challenger", "champion"):
        checks = _gate_challenger_or_champion(ctx)
        passed = all(checks.values())
    elif target_status == "tiny_live_allowed":
        checks = _gate_tiny_live(ctx)
        passed = all(checks.values())
    elif target_status == "disabled":
        checks = _gate_disabled(ctx)
        passed = any(checks.values())  # OR semantics
    elif target_status == "research_only":
        checks = {"always": True}
        passed = True
    else:  # pragma: no cover
        checks = {}
        passed = False
    return GateResult(target_status=target_status, passed=passed, checks=checks)
