"""Strict pydantic ingest models (unknown fields rejected → boring & safe)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaperTradeIn(_Strict):
    client_uuid: str = Field(min_length=1)
    candidate_id: str | None = None
    market_id: int | None = None
    token_id: str | None = None
    side: str | None = None
    entry_style: str | None = None
    entry_price: float | None = None
    size_usd: float | None = None
    fee_paid: float | None = None
    outcome: str | None = None
    pnl: float | None = None
    opened_at: str | None = None
    closed_at: str | None = None


class BotDecisionIn(_Strict):
    client_uuid: str = Field(min_length=1)
    candidate_id: str | None = None
    market_id: int | None = None
    decision: str | None = None
    reason: str | None = None
    decided_at: str | None = None


class BotHeartbeatIn(_Strict):
    client_uuid: str = Field(min_length=1)
    bot_id: str | None = None
    bot_status: str | None = None
    bot_mode: str | None = None
    sent_at: str | None = None
