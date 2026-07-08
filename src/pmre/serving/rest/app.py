"""FastAPI REST facade (bot data plane + ingest API).

Read endpoints use a read-scope bearer token; ingest endpoints use a separate
ingest-scope token (hashed in ``ingest_tokens``). Every read response is wrapped
in the freshness envelope. Bind to the WireGuard/Tailscale overlay IP only.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import ORJSONResponse

from ...config import Settings
from ...logging_setup import get_logger
from ..auth import RateLimiter, verify_ingest_token, verify_read_token
from ..envelope import build_envelope
from ..ingest import IngestService
from ..service import ResearchService
from .schemas import BotDecisionIn, BotHeartbeatIn, PaperTradeIn

log = get_logger("serving.rest")


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def create_app(session_factory, settings: Settings | None = None, health=None) -> FastAPI:
    settings = settings or Settings()
    service = ResearchService(session_factory, settings)
    ingest = IngestService(session_factory)
    read_limiter = RateLimiter(limit=600, window_s=60)
    ingest_limiter = RateLimiter(limit=1200, window_s=60)

    app = FastAPI(
        title="pm-research-engine REST", version="v1", default_response_class=ORJSONResponse,
    )

    # --- auth dependencies ------------------------------------------------
    async def require_read(request: Request, authorization: str | None = Header(default=None)):
        token = _bearer(authorization)
        if not verify_read_token(token, settings.read_tokens, settings.mcp_bearer_token):
            log.warning("unauthorized_read", path=request.url.path)
            if health:
                health.warning("rest", f"unauthorized read {request.url.path}")
            raise HTTPException(status_code=401, detail="invalid or missing read token")
        if not read_limiter.allow(token):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return token

    async def require_ingest(request: Request, authorization: str | None = Header(default=None)):
        token = _bearer(authorization)
        if not verify_ingest_token(token, session_factory):
            log.warning("unauthorized_ingest", path=request.url.path)
            if health:
                health.warning("rest", f"unauthorized ingest {request.url.path}")
            raise HTTPException(status_code=401, detail="invalid or missing ingest token")
        if not ingest_limiter.allow(token):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return token

    def envelope(data, warnings=None, evidence=None):
        return build_envelope(
            data, data_last_updated_at=service.last_update_ts(),
            warnings=warnings, evidence=evidence,
        )

    # --- health (no auth) -------------------------------------------------
    @app.get("/v1/health")
    async def health_endpoint():
        return envelope(service.get_system_health())

    # --- read endpoints ---------------------------------------------------
    @app.get("/v1/session/current", dependencies=[Depends(require_read)])
    async def session_current():
        return envelope(service.get_current_session())

    @app.get("/v1/markets/current", dependencies=[Depends(require_read)])
    async def markets_current():
        data = service.get_current_btc5m_market()
        warnings = [] if data else ["no active market found"]
        return envelope(data, warnings=warnings)

    @app.get("/v1/snapshots/latest", dependencies=[Depends(require_read)])
    async def snapshots_latest(market_id: int | None = None, label: str | None = None):
        return envelope(service.get_latest_market_snapshot(market_id, label))

    @app.get("/v1/performance/timestamps", dependencies=[Depends(require_read)])
    async def performance_timestamps(entry_style: str | None = None, scope: str | None = None,
                                     label: str | None = None):
        return envelope(service.get_timestamp_performance(entry_style, scope, label))

    @app.get("/v1/performance/calibration", dependencies=[Depends(require_read)])
    async def performance_calibration(label: str | None = None, scope: str = "total"):
        return envelope(service.get_calibration_curve(label, scope))

    @app.get("/v1/performance/sessions", dependencies=[Depends(require_read)])
    async def performance_sessions(label: str | None = None):
        return envelope(service.get_session_performance(label))

    @app.get("/v1/performance/regimes", dependencies=[Depends(require_read)])
    async def performance_regimes(label: str | None = None):
        return envelope(service.get_regime_performance(label))

    @app.get("/v1/candidates", dependencies=[Depends(require_read)])
    async def candidates(status: str | None = None):
        return envelope(service.get_strategy_candidates(status))

    @app.get("/v1/candidates/champion", dependencies=[Depends(require_read)])
    async def champion():
        return envelope(service.get_champion_strategy())

    @app.get("/v1/fills/maker-estimates", dependencies=[Depends(require_read)])
    async def maker_estimates(label: str | None = None, offset: int | None = None):
        return envelope(service.get_maker_fill_estimates(label, offset))

    @app.get("/v1/fairvalue/params", dependencies=[Depends(require_read)])
    async def fairvalue_params(market_id: int, label: str | None = None):
        return envelope(service.get_fair_value_snapshot(market_id, label))

    @app.get("/v1/fees/parameters", dependencies=[Depends(require_read)])
    async def fee_params(market_id: int):
        return envelope(service.get_fee_parameters(market_id))

    @app.get("/v1/quality/report", dependencies=[Depends(require_read)])
    async def quality_report(window: int = 24):
        return envelope(service.get_data_quality_report(window))

    @app.get("/v1/analysis/summary", dependencies=[Depends(require_read)])
    async def analysis_summary(run_id: str | None = None):
        return envelope(service.get_analysis_run_summary(run_id))

    # --- ingest endpoints -------------------------------------------------
    @app.post("/v1/ingest/paper-trades", dependencies=[Depends(require_ingest)])
    async def ingest_paper_trades(payload: PaperTradeIn):
        return ingest.ingest_paper_trade(payload.model_dump())

    @app.post("/v1/ingest/bot-decisions", dependencies=[Depends(require_ingest)])
    async def ingest_bot_decisions(payload: BotDecisionIn):
        return ingest.ingest_bot_decision(payload.model_dump())

    @app.post("/v1/ingest/bot-heartbeat", dependencies=[Depends(require_ingest)])
    async def ingest_bot_heartbeat(payload: BotHeartbeatIn):
        return ingest.ingest_heartbeat(payload.model_dump())

    return app
