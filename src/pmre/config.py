"""Runtime configuration (pydantic-settings).

Dev is permissive so the suite and a laptop can boot with zero env. Production
(``PMRE_ENV=production``) *fails fast* on missing security-critical secrets — the
Phase-0 acceptance test asserts exactly this behaviour.
"""

from __future__ import annotations

import datetime as dt

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import (
    FEE_MODEL_VERSION,
    REGIME_MODEL_VERSION,
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing/invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PMRE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = Field(default="dev", description="dev | staging | production")

    # --- Storage -----------------------------------------------------------
    database_url: str = Field(
        default="sqlite+pysqlite:///:memory:",
        description="SQLAlchemy URL. Postgres+TimescaleDB in prod, SQLite for tests/dev.",
    )
    data_dir: str = Field(default="data")
    parquet_dir: str = Field(default="data/parquet")
    raw_dir: str = Field(default="data/raw")

    # --- Polymarket endpoints ---------------------------------------------
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_agent: str = "pm-research-engine/0.1 (+research; contact set in prod)"

    # --- BTC proxy feeds ---------------------------------------------------
    binance_spot_ws: str = "wss://stream.binance.com:9443/ws"
    binance_perp_ws: str = "wss://fstream.binance.com/ws"
    coinbase_ws: str = "wss://ws-feed.exchange.coinbase.com"

    # --- Model versions ----------------------------------------------------
    fee_model_version: str = FEE_MODEL_VERSION
    regime_model_version: str = REGIME_MODEL_VERSION

    # --- Snapshot / collector tuning --------------------------------------
    snapshot_offsets_s: tuple[int, ...] = (270, 240, 210, 180, 150, 120, 90, 60, 30)
    market_period_s: int = 300
    clock_drift_abort_ms: int = 250
    clock_drift_warn_ms: int = 50
    stale_book_s: float = 10.0
    close_call_bps: float = 2.0
    divergence_flag_bps: float = 10.0
    sigma_floor: float = 1e-6  # EWMA sigma floor so z doesn't explode when BTC goes quiet

    # --- Fee curve ---------------------------------------------------------
    # Peak taker cost ~ $1.80 / 100 shares at mid; fee = rate * shares * p*(1-p).
    # rate chosen so peak (p=0.5) ≈ 0.018/share = $1.80/100. verify vs live docs.
    default_fee_rate: float = 0.072
    maker_rebate_rate: float = 0.0

    # --- Analytics floors --------------------------------------------------
    min_n_bucket: int = 200
    min_n_promote: int = 500
    fdr_q: float = 0.10

    # --- Serving secrets (required in production) --------------------------
    rest_bearer_tokens: str = Field(default="", description="comma-separated read tokens")
    mcp_bearer_token: str = ""
    ingest_bearer_token: str = ""
    serving_host: str = "127.0.0.1"
    rest_port: int = 8080
    mcp_port: int = 8090

    # --- Alerts ------------------------------------------------------------
    alert_telegram_bot_token: str = ""
    alert_telegram_chat_id: str = ""

    @field_validator("snapshot_offsets_s", mode="before")
    @classmethod
    def _parse_offsets(cls, v):
        if isinstance(v, str):
            return tuple(int(x) for x in v.split(",") if x.strip())
        return v

    @property
    def read_tokens(self) -> set[str]:
        return {t.strip() for t in self.rest_bearer_tokens.split(",") if t.strip()}

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"prod", "production"}

    def require_production_secrets(self) -> None:
        """Fail fast on missing secrets in production."""
        missing: list[str] = []
        if not self.read_tokens:
            missing.append("PMRE_REST_BEARER_TOKENS")
        if not self.mcp_bearer_token:
            missing.append("PMRE_MCP_BEARER_TOKEN")
        if not self.ingest_bearer_token:
            missing.append("PMRE_INGEST_BEARER_TOKEN")
        if not self.alert_telegram_bot_token or not self.alert_telegram_chat_id:
            missing.append("PMRE_ALERT_TELEGRAM_BOT_TOKEN/PMRE_ALERT_TELEGRAM_CHAT_ID")
        if self.database_url.startswith("sqlite"):
            missing.append("PMRE_DATABASE_URL (postgres required in production)")
        if missing:
            raise ConfigError(
                "Missing required production configuration: " + ", ".join(missing)
            )

    def utcnow(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


def load_settings(**overrides) -> Settings:
    """Load settings, validating production secrets when ``env`` is production."""
    settings = Settings(**overrides)
    if settings.is_production:
        settings.require_production_secrets()
    return settings
