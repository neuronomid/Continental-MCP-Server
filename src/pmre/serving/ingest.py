"""Ingest service — the ONLY write path (bot telemetry tables only).

Idempotent on the client-supplied UUID: a duplicate submission is a no-op that
still returns success (mcp_phases.md Phase 8). Schemas are strict — unknown
fields are rejected at the API boundary via pydantic models.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from ..db.models import BotDecisionLog, BotHeartbeat, PaperTrade


def _parse_dt(v):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))


class IngestService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _insert_idempotent(self, model, client_uuid: str, build) -> tuple[bool, int]:
        with self.session_factory() as s:
            existing = s.execute(
                select(model).where(model.client_uuid == client_uuid)
            ).scalar_one_or_none()
            if existing is not None:
                return False, existing.id
            obj = build()
            s.add(obj)
            s.commit()
            s.refresh(obj)
            return True, obj.id

    def ingest_paper_trade(self, rec: dict) -> dict:
        def build():
            return PaperTrade(
                client_uuid=rec["client_uuid"], candidate_id=rec.get("candidate_id"),
                market_id=rec.get("market_id"), token_id=rec.get("token_id"),
                side=rec.get("side"), entry_style=rec.get("entry_style"),
                entry_price=rec.get("entry_price"), size_usd=rec.get("size_usd"),
                fee_paid=rec.get("fee_paid"), outcome=rec.get("outcome"), pnl=rec.get("pnl"),
                opened_at=_parse_dt(rec.get("opened_at")), closed_at=_parse_dt(rec.get("closed_at")),
                raw_json=rec,
            )
        created, rid = self._insert_idempotent(PaperTrade, rec["client_uuid"], build)
        return {"created": created, "id": rid, "client_uuid": rec["client_uuid"]}

    def ingest_bot_decision(self, rec: dict) -> dict:
        def build():
            return BotDecisionLog(
                client_uuid=rec["client_uuid"], candidate_id=rec.get("candidate_id"),
                market_id=rec.get("market_id"), decision=rec.get("decision"),
                reason=rec.get("reason"), decided_at=_parse_dt(rec.get("decided_at")),
                raw_json=rec,
            )
        created, rid = self._insert_idempotent(BotDecisionLog, rec["client_uuid"], build)
        return {"created": created, "id": rid, "client_uuid": rec["client_uuid"]}

    def ingest_heartbeat(self, rec: dict) -> dict:
        def build():
            return BotHeartbeat(
                client_uuid=rec["client_uuid"], bot_id=rec.get("bot_id"),
                bot_status=rec.get("bot_status"), bot_mode=rec.get("bot_mode"),
                sent_at=_parse_dt(rec.get("sent_at")), raw_json=rec,
            )
        created, rid = self._insert_idempotent(BotHeartbeat, rec["client_uuid"], build)
        return {"created": created, "id": rid, "client_uuid": rec["client_uuid"]}
