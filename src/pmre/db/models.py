"""SQLAlchemy 2.x ORM models — the full v1 schema + v2/v2.1 additions.

Designed to run on both PostgreSQL 16 + TimescaleDB (production) and SQLite
(tests/dev). Hypertable/compression policies are applied separately (Postgres
only) via ``engine.apply_timescale_policies``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .types import UTCDateTime


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, dict: JSON}


def _now_col():
    return mapped_column(UTCDateTime, server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Discovery / markets
# ---------------------------------------------------------------------------
class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_type: Mapped[str] = mapped_column(String(32), default="btc_updown_5m")
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    condition_id: Mapped[str | None] = mapped_column(String(80), index=True)
    question_id: Mapped[str | None] = mapped_column(String(80))
    event_slug: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)

    slug_derived_start_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    window_start_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    expected_resolution_time_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    market_end_time_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    enable_order_book: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)

    # v2 additive columns
    fees_enabled: Mapped[bool | None] = mapped_column(Boolean)
    fee_rate_bps: Mapped[float | None] = mapped_column(Float)
    fee_params_json: Mapped[dict | None] = mapped_column(JSON)
    tick_size: Mapped[float | None] = mapped_column(Float)
    min_order_size: Mapped[float | None] = mapped_column(Float)
    price_to_beat: Mapped[float | None] = mapped_column(Float)
    price_to_beat_source: Mapped[str | None] = mapped_column(String(48))

    discovered_via: Mapped[str | None] = mapped_column(String(24))  # slug | tag_scan
    collector_version: Mapped[str | None] = mapped_column(String(32))
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = _now_col()

    tokens: Mapped[list[MarketToken]] = relationship(back_populates="market")


class MarketToken(Base):
    __tablename__ = "market_tokens"
    __table_args__ = (UniqueConstraint("market_id", "outcome", name="uq_market_outcome"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(120), index=True)
    outcome: Mapped[str] = mapped_column(String(8))  # UP | DOWN
    outcome_index: Mapped[int | None] = mapped_column(Integer)
    is_winner: Mapped[bool | None] = mapped_column(Boolean)

    market: Mapped[Market] = relationship(back_populates="tokens")


class FeeSchedule(Base):
    """Per-market fee param history (fees can change; keep every capture)."""

    __tablename__ = "fee_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    fees_enabled: Mapped[bool | None] = mapped_column(Boolean)
    fee_rate_bps: Mapped[float | None] = mapped_column(Float)
    maker_rebate_bps: Mapped[float | None] = mapped_column(Float)
    fee_params_json: Mapped[dict | None] = mapped_column(JSON)
    fee_model_version: Mapped[str | None] = mapped_column(String(32))
    captured_at: Mapped[dt.datetime] = _now_col()


# ---------------------------------------------------------------------------
# Session calendar
# ---------------------------------------------------------------------------
class SessionCalendar(Base):
    __tablename__ = "session_calendar"
    __table_args__ = (
        UniqueConstraint("calendar_date", "session", name="uq_calendar_date_session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calendar_date: Mapped[dt.date] = mapped_column(index=True)
    session: Mapped[str] = mapped_column(String(24))
    open_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    close_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    integrity: Mapped[str] = mapped_column(String(16))  # regular|holiday|half_day|weekend
    source_calendar: Mapped[str | None] = mapped_column(String(8))
    session_model_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = _now_col()


# ---------------------------------------------------------------------------
# Market data: books, snapshots, trades, btc
# ---------------------------------------------------------------------------
class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("market_id", "label", name="uq_snapshot_market_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    label: Mapped[str] = mapped_column(String(8))  # t_270 ... t_30
    target_seconds_left: Mapped[int] = mapped_column(Integer)
    snapshot_actual_seconds_left: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)

    # Book-derived
    up_best_bid: Mapped[float | None] = mapped_column(Float)
    up_best_ask: Mapped[float | None] = mapped_column(Float)
    down_best_bid: Mapped[float | None] = mapped_column(Float)
    down_best_ask: Mapped[float | None] = mapped_column(Float)
    up_mid: Mapped[float | None] = mapped_column(Float)
    down_mid: Mapped[float | None] = mapped_column(Float)
    up_spread: Mapped[float | None] = mapped_column(Float)
    market_spread_proxy: Mapped[float | None] = mapped_column(Float)  # up_ask+down_ask-1

    dominant_side: Mapped[str | None] = mapped_column(String(8))  # UP|DOWN
    dominant_mid: Mapped[float | None] = mapped_column(Float)
    dominant_ask: Mapped[float | None] = mapped_column(Float)
    last_trade_price: Mapped[float | None] = mapped_column(Float)

    # Simulated execution (per notional in USD)
    vwap_buy_1: Mapped[float | None] = mapped_column(Float)
    vwap_buy_2: Mapped[float | None] = mapped_column(Float)
    vwap_buy_5: Mapped[float | None] = mapped_column(Float)
    vwap_buy_10: Mapped[float | None] = mapped_column(Float)
    slippage_10: Mapped[float | None] = mapped_column(Float)
    max_usd_buy_within_2c: Mapped[float | None] = mapped_column(Float)
    depth_up_2c: Mapped[float | None] = mapped_column(Float)
    depth_down_2c: Mapped[float | None] = mapped_column(Float)

    taker_fee_est_dominant: Mapped[float | None] = mapped_column(Float)
    net_ev_inputs_version: Mapped[str | None] = mapped_column(String(32))
    up_tick_size: Mapped[float | None] = mapped_column(Float)

    # BTC / fair value (Phase 3)
    btc_price: Mapped[float | None] = mapped_column(Float)
    btc_distance_from_start: Mapped[float | None] = mapped_column(Float)
    btc_distance_bps: Mapped[float | None] = mapped_column(Float)
    sigma_1s: Mapped[float | None] = mapped_column(Float)
    z_score: Mapped[float | None] = mapped_column(Float)
    p_fair: Mapped[float | None] = mapped_column(Float)
    model_edge: Mapped[float | None] = mapped_column(Float)
    btc_source_divergence_bps: Mapped[float | None] = mapped_column(Float)
    ret_5s: Mapped[float | None] = mapped_column(Float)
    ret_30s: Mapped[float | None] = mapped_column(Float)
    ret_60s: Mapped[float | None] = mapped_column(Float)
    feature_quality: Mapped[str | None] = mapped_column(String(16))  # ok|degraded|missing

    # Session
    session_primary: Mapped[str | None] = mapped_column(String(24))
    session_overlap: Mapped[str | None] = mapped_column(String(24))
    session_integrity: Mapped[str | None] = mapped_column(String(16))
    session_model_version: Mapped[str | None] = mapped_column(String(32))

    # Quality flags
    stale_book_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    crossed_book_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    bad_sum_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    divergence_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # Back-labeled after resolution (Phase 4)
    was_correct_mid: Mapped[bool | None] = mapped_column(Boolean)
    was_correct_ask: Mapped[bool | None] = mapped_column(Boolean)
    was_correct_last_trade: Mapped[bool | None] = mapped_column(Boolean)
    was_close_call: Mapped[bool | None] = mapped_column(Boolean)

    regime: Mapped[str | None] = mapped_column(String(24))
    collector_version: Mapped[str | None] = mapped_column(String(32))
    feature_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = _now_col()


class OrderbookLevel(Base):
    __tablename__ = "orderbook_levels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(120))
    outcome: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(4))  # bid | ask
    level: Mapped[int] = mapped_column(Integer)  # 0..9
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)


class TradePrint(Base):
    __tablename__ = "trade_prints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(120), index=True)
    outcome: Mapped[str | None] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    side: Mapped[str | None] = mapped_column(String(8))
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    seconds_left: Mapped[float | None] = mapped_column(Float)


class BtcTick(Base):
    """1s bar (hypertable in prod)."""

    __tablename__ = "btc_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    source: Mapped[str] = mapped_column(String(16))  # binance_spot|binance_perp|coinbase
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    trade_count: Mapped[int | None] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
class MarketResolution(Base):
    __tablename__ = "market_resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), unique=True, index=True)
    winning_outcome: Mapped[str] = mapped_column(String(8))  # UP|DOWN
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    resolution_source: Mapped[str | None] = mapped_column(String(32))

    price_to_beat: Mapped[float | None] = mapped_column(Float)
    oracle_end_price: Mapped[float | None] = mapped_column(Float)
    proxy_end_price: Mapped[float | None] = mapped_column(Float)
    proxy_oracle_divergence_bps: Mapped[float | None] = mapped_column(Float)
    margin_bps: Mapped[float | None] = mapped_column(Float)
    tie_rule_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    was_close_call: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = _now_col()


# ---------------------------------------------------------------------------
# Analytics outputs
# ---------------------------------------------------------------------------
class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # hourly|daily|weekly
    started_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    window_start: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    window_end: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    n_markets: Mapped[int | None] = mapped_column(Integer)
    n_dropped: Mapped[int | None] = mapped_column(Integer)
    fee_model_version: Mapped[str | None] = mapped_column(String(32))
    regime_model_version: Mapped[str | None] = mapped_column(String(32))
    feature_version: Mapped[str | None] = mapped_column(String(32))
    summary_json: Mapped[dict | None] = mapped_column(JSON)
    summary_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="ok")


class CalibrationBin(Base):
    __tablename__ = "calibration_bins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(48), index=True)
    label: Mapped[str] = mapped_column(String(8))
    price_bin_lo: Mapped[float] = mapped_column(Float)
    price_bin_hi: Mapped[float] = mapped_column(Float)
    scope: Mapped[str] = mapped_column(String(48))  # total | session:<x> | overlap:<x>
    session_integrity_filter: Mapped[str] = mapped_column(String(16), default="regular")
    regime: Mapped[str | None] = mapped_column(String(24))
    n: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float | None] = mapped_column(Float)
    avg_entry_price: Mapped[float | None] = mapped_column(Float)
    wilson_lo: Mapped[float | None] = mapped_column(Float)
    wilson_hi: Mapped[float | None] = mapped_column(Float)
    edge: Mapped[float | None] = mapped_column(Float)  # win_rate - avg_entry_price
    p_value: Mapped[float | None] = mapped_column(Float)
    fdr_pass: Mapped[bool] = mapped_column(Boolean, default=False)


class TimestampPerformance(Base):
    __tablename__ = "timestamp_performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(48), index=True)
    label: Mapped[str] = mapped_column(String(8))
    scope: Mapped[str] = mapped_column(String(48))
    session_integrity_filter: Mapped[str] = mapped_column(String(16), default="regular")
    entry_style: Mapped[str] = mapped_column(String(24))  # taker_ask|maker_mid|maker_join_bid
    direction: Mapped[str] = mapped_column(String(16), default="dominant")  # dominant|contrarian
    regime: Mapped[str | None] = mapped_column(String(24))
    n: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float | None] = mapped_column(Float)
    avg_price: Mapped[float | None] = mapped_column(Float)
    edge_vs_price: Mapped[float | None] = mapped_column(Float)
    net_ev_taker: Mapped[float | None] = mapped_column(Float)
    net_ev_maker: Mapped[float | None] = mapped_column(Float)
    net_ev_ci_lower_95: Mapped[float | None] = mapped_column(Float)
    brier: Mapped[float | None] = mapped_column(Float)
    log_loss: Mapped[float | None] = mapped_column(Float)
    fill_prob_maker: Mapped[float | None] = mapped_column(Float)
    median_time_to_fill_s: Mapped[float | None] = mapped_column(Float)
    fdr_pass: Mapped[bool] = mapped_column(Boolean, default=False)


class MakerFillEstimate(Base):
    __tablename__ = "maker_fill_estimates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(48), index=True)
    snapshot_label: Mapped[str] = mapped_column(String(8))
    offset_ticks: Mapped[int] = mapped_column(Integer)  # 0=join bid, negative=through
    post_style: Mapped[str] = mapped_column(String(24))  # join_bid|mid_minus_1tick|mid
    regime: Mapped[str | None] = mapped_column(String(24))
    p_fill: Mapped[float] = mapped_column(Float)
    median_ttf_s: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class StrategyCandidate(Base):
    __tablename__ = "strategy_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="research_only")
    label: Mapped[str] = mapped_column(String(8))
    price_bin_lo: Mapped[float | None] = mapped_column(Float)
    price_bin_hi: Mapped[float | None] = mapped_column(Float)
    scope: Mapped[str] = mapped_column(String(48), default="total")
    entry_style: Mapped[str] = mapped_column(String(24))
    direction: Mapped[str] = mapped_column(String(16), default="dominant")
    regime: Mapped[str | None] = mapped_column(String(24))
    filters_json: Mapped[dict | None] = mapped_column(JSON)

    n: Mapped[int | None] = mapped_column(Integer)
    win_rate: Mapped[float | None] = mapped_column(Float)
    net_ev: Mapped[float | None] = mapped_column(Float)
    net_ev_ci_lower_95: Mapped[float | None] = mapped_column(Float)
    edge: Mapped[float | None] = mapped_column(Float)
    median_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    fill_prob_maker: Mapped[float | None] = mapped_column(Float)

    fdr_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    walk_forward_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    model_edge_gate: Mapped[float | None] = mapped_column(Float)
    fee_model_version: Mapped[str | None] = mapped_column(String(32))
    regime_model_version: Mapped[str | None] = mapped_column(String(32))
    evidence_json: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[dt.datetime] = _now_col()
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )


class CandidateAuditLog(Base):
    __tablename__ = "candidate_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(32), default="engine")  # engine|human
    at: Mapped[dt.datetime] = _now_col()
    details_json: Mapped[dict | None] = mapped_column(JSON)


# ---------------------------------------------------------------------------
# Ingest (bot telemetry) — the only write path from the bot
# ---------------------------------------------------------------------------
class IngestToken(Base):
    __tablename__ = "ingest_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(24), default="ingest")
    label: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = _now_col()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), index=True)
    market_id: Mapped[int | None] = mapped_column(Integer)
    token_id: Mapped[str | None] = mapped_column(String(120))
    side: Mapped[str | None] = mapped_column(String(8))
    entry_style: Mapped[str | None] = mapped_column(String(24))
    entry_price: Mapped[float | None] = mapped_column(Float)
    size_usd: Mapped[float | None] = mapped_column(Float)
    fee_paid: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(8))
    pnl: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    received_at: Mapped[dt.datetime] = _now_col()
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class BotDecisionLog(Base):
    __tablename__ = "bot_decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), index=True)
    market_id: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str | None] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    received_at: Mapped[dt.datetime] = _now_col()
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class BotHeartbeat(Base):
    __tablename__ = "bot_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_uuid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    bot_id: Mapped[str | None] = mapped_column(String(48))
    bot_status: Mapped[str | None] = mapped_column(String(32))
    bot_mode: Mapped[str | None] = mapped_column(String(32))
    sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    received_at: Mapped[dt.datetime] = _now_col()
    raw_json: Mapped[dict | None] = mapped_column(JSON)


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
class SystemHealthEvent(Base):
    __tablename__ = "system_health_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(48), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # heartbeat|warning|critical
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[dict | None] = mapped_column(JSON)
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)


ALL_TABLES = Base.metadata.sorted_tables

# Tables that become Timescale hypertables in production.
HYPERTABLES = {
    "btc_ticks": "ts",
    "trade_prints": "ts",
    "orderbook_levels": "id",  # partition by a synthetic time via snapshot in prod migration
    "system_health_events": "ts",
}
