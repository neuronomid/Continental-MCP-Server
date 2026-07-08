"""Bearer-token auth: read scope (settings) vs ingest scope (hashed in DB)."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import time

from sqlalchemy import select

from ..db.models import IngestToken


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_read_token(token: str | None, read_tokens: set[str], mcp_token: str | None = None) -> bool:
    if not token:
        return False
    if token in read_tokens:
        return True
    return bool(mcp_token) and secrets.compare_digest(token, mcp_token)


def verify_mcp_token(token: str | None, mcp_token: str | None) -> bool:
    return bool(token) and bool(mcp_token) and secrets.compare_digest(token, mcp_token)


def create_ingest_token(session_factory, label: str | None = None, token: str | None = None) -> str:
    token = token or secrets.token_urlsafe(24)
    with session_factory() as s:
        s.add(IngestToken(token_hash=hash_token(token), scope="ingest", label=label))
        s.commit()
    return token


def verify_ingest_token(token: str | None, session_factory) -> bool:
    if not token:
        return False
    h = hash_token(token)
    with session_factory() as s:
        row = s.execute(
            select(IngestToken).where(IngestToken.token_hash == h)
        ).scalar_one_or_none()
        return row is not None and row.revoked_at is None


def revoke_ingest_token(session_factory, token: str) -> None:
    h = hash_token(token)
    with session_factory() as s:
        row = s.execute(select(IngestToken).where(IngestToken.token_hash == h)).scalar_one_or_none()
        if row:
            row.revoked_at = dt.datetime.now(dt.UTC)
            s.commit()


class RateLimiter:
    """Fixed-window per-token limiter (adequate on a private overlay network)."""

    def __init__(self, limit: int = 120, window_s: float = 60.0):
        self.limit = limit
        self.window_s = window_s
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= self.window_s:
            self._buckets[key] = (now, 1)
            return True
        if count >= self.limit:
            return False
        self._buckets[key] = (start, count + 1)
        return True
