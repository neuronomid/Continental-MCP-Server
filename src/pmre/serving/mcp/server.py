"""MCP server (FastMCP, Streamable HTTP) — the agent facade.

Delegates to the *same* :class:`ResearchService` as the REST facade, so agents
and the bot see identical evidence. Every tool response is wrapped, via a shared
decorator, in the freshness envelope carrying ``current_session`` and
``session_integrity`` (mcp_plan.md §7.2). Tools are read-only: no tool can reach
a write path. Framed as "evidence, not advice".
"""

from __future__ import annotations

import functools

from mcp.server.fastmcp import FastMCP

from ...config import Settings
from ..auth import verify_mcp_token
from ..envelope import build_envelope
from ..service import ResearchService

EVIDENCE_NOTE = (
    "This is evidence (data with n, confidence intervals and freshness), not "
    "trading advice. Decisions are made on CI lower bounds, never point estimates."
)


def create_mcp_server(session_factory, settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings()
    service = ResearchService(session_factory, settings)
    mcp = FastMCP(name="pm-research-engine", instructions=EVIDENCE_NOTE, host=settings.serving_host)

    def enveloped(fn):
        """Wrap a data-returning function so it always ships the full envelope."""

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if isinstance(result, tuple):
                data, warnings = result
            else:
                data, warnings = result, None
            return build_envelope(
                data, data_last_updated_at=service.last_update_ts(), warnings=warnings,
            )

        return wrapper

    # --- tools (§7.2) -----------------------------------------------------
    @mcp.tool()
    @enveloped
    def get_system_health() -> dict:
        """Overall engine health: which collectors are alive/silent + recent incidents."""
        return service.get_system_health()

    @mcp.tool()
    @enveloped
    def get_current_session() -> dict:
        """Current trading session: primary/overlap, integrity, seconds to next boundary."""
        return service.get_current_session()

    @mcp.tool()
    @enveloped
    def get_current_btc5m_market() -> dict:
        """Active + next BTC-5m market with token ids, price-to-beat, fee params, tick size."""
        data = service.get_current_btc5m_market()
        return (data, None if data else ["no active market"])

    @mcp.tool()
    @enveloped
    def get_latest_market_snapshot(market_id: int | None = None, label: str | None = None) -> dict:
        """Latest order-book snapshot (mids, spreads, depth, fee estimate) for a market/label."""
        return service.get_latest_market_snapshot(market_id, label)

    @mcp.tool()
    @enveloped
    def get_timestamp_performance(
        entry_style: str | None = None, scope: str | None = None, label: str | None = None
    ) -> dict:
        """Per-timestamp performance (win rate, net EV taker/maker, CI-lower, Brier) with n and CIs.
        Total and per-session scopes are returned side by side unless a scope is requested."""
        return service.get_timestamp_performance(entry_style, scope, label)

    @mcp.tool()
    @enveloped
    def get_calibration_curve(label: str | None = None, scope: str = "total") -> dict:
        """Reliability curve: empirical win rate vs price per 2¢ bin, with Wilson CIs and FDR flags."""
        return service.get_calibration_curve(label, scope)

    @mcp.tool()
    @enveloped
    def get_session_performance(label: str | None = None) -> dict:
        """Performance broken out per session/overlap (each with its own n and CI)."""
        return service.get_session_performance(label)

    @mcp.tool()
    @enveloped
    def get_regime_performance(label: str | None = None) -> dict:
        """Performance broken out per volatility regime."""
        return service.get_regime_performance(label)

    @mcp.tool()
    @enveloped
    def get_fee_parameters(market_id: int) -> dict:
        """Fee parameters for a market (feesEnabled, rate, fee_model_version) + latest history row."""
        return service.get_fee_parameters(market_id)

    @mcp.tool()
    @enveloped
    def get_fair_value_snapshot(market_id: int, label: str | None = None) -> dict:
        """Fair-value model output for a snapshot: p_fair, z_score, sigma_1s, model_edge."""
        return service.get_fair_value_snapshot(market_id, label)

    @mcp.tool()
    @enveloped
    def get_maker_fill_estimates(label: str | None = None, offset: int | None = None) -> dict:
        """P(fill | label, post style, regime) and time-to-fill for hypothetical maker posts."""
        return service.get_maker_fill_estimates(label, offset)

    @mcp.tool()
    @enveloped
    def get_strategy_candidates(status: str | None = None) -> dict:
        """Strategy candidates with status and full evidence (n, CIs, net EVs, liquidity)."""
        return service.get_strategy_candidates(status)

    @mcp.tool()
    @enveloped
    def get_champion_strategy() -> dict:
        """The current champion strategy and its CI-lower net EV (or null if none)."""
        return service.get_champion_strategy()

    @mcp.tool()
    @enveloped
    def get_paper_trade_performance(candidate_id: str | None = None) -> dict:
        """Aggregated paper-trade telemetry (n, net PnL, win rate) ingested from the bot."""
        return service.get_paper_trade_performance(candidate_id)

    @mcp.tool()
    @enveloped
    def get_analysis_run_summary(run_id: str | None = None) -> dict:
        """Summary of an analysis run (or the latest): counts, versions, deterministic hash."""
        return service.get_analysis_run_summary(run_id)

    @mcp.tool()
    @enveloped
    def get_data_quality_report(window: int = 24) -> dict:
        """Data-quality report: snapshot counts, stale/crossed/close-call rates over a window."""
        return service.get_data_quality_report(window)

    # --- resources --------------------------------------------------------
    @mcp.resource("pmre://methodology")
    def methodology() -> str:
        """Methodology notes: calibration-first, CI-lower decisions, BH-FDR, dual-scope sessions."""
        return (
            "PMRE methodology: (1) calibration-first — reliability curves, not raw accuracy; "
            "(2) all decisions on Wilson CI-lower net EV after fees; (3) BH-FDR (q=0.10) on every "
            "bucket claim; (4) fees are first-class (dynamic taker curve, maker family); "
            "(5) fair value Φ(z) as an independent benchmark; (6) dual-scope (total + per session, "
            "regular-integrity default). This is evidence, not advice."
        )

    @mcp.resource("pmre://data-dictionary")
    def data_dictionary() -> str:
        """Schema / data dictionary for the served tables and fields."""
        return (
            "markets, market_tokens, snapshots (mids/spreads/depth/p_fair/model_edge/session), "
            "calibration_bins, timestamp_performance (net_ev_taker/maker, ci_lower, brier, fdr_pass), "
            "maker_fill_estimates, strategy_candidates, fee_schedules, session_calendar."
        )

    @mcp.resource("pmre://daily-report")
    def daily_report() -> str:
        """The latest daily research report (markdown)."""
        from ...analytics.reports import generate_daily_report

        return generate_daily_report(session_factory)

    @mcp.resource("pmre://fee-model")
    def fee_model() -> str:
        """Fee model notes."""
        return (
            f"Taker fee per share ≈ rate·p·(1−p), peak≈$1.80/100sh at p=0.5 "
            f"(fee_model_version={settings.fee_model_version}). Makers pay zero; rebate optional. "
            "Fee schedules are per-market and versioned; history is retained."
        )

    return mcp


def build_bearer_middleware(settings: Settings):
    """Starlette middleware factory enforcing the MCP bearer token on the HTTP app."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class BearerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth = request.headers.get("authorization", "")
            token = auth.split()[1] if auth.lower().startswith("bearer ") else None
            if not verify_mcp_token(token, settings.mcp_bearer_token):
                return JSONResponse({"error": "invalid or missing MCP bearer token"}, status_code=401)
            return await call_next(request)

    return BearerMiddleware


def create_http_app(session_factory, settings: Settings | None = None):
    """Streamable-HTTP ASGI app with bearer auth (bind to overlay IP only)."""
    settings = settings or Settings()
    mcp = create_mcp_server(session_factory, settings)
    app = mcp.streamable_http_app()
    app.add_middleware(build_bearer_middleware(settings))
    return app
