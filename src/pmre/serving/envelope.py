"""Response envelope: freshness + evidence + current session on every response.

Every REST/MCP response carries ``generated_at``, ``data_last_updated_at``,
``staleness_s``, ``warnings[]`` and — per mcp_plan.md §7.2 — ``current_session``
and ``session_integrity``. Enforced centrally so no endpoint can ship a bare
payload.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pm_sessions import label_instant

REQUIRED_ENVELOPE_KEYS = {
    "generated_at",
    "data_last_updated_at",
    "staleness_s",
    "warnings",
    "current_session",
    "session_integrity",
    "session_model_version",
    "data",
}


def build_envelope(
    data: Any,
    data_last_updated_at: dt.datetime | None = None,
    warnings: list[str] | None = None,
    now: dt.datetime | None = None,
    session_provider=None,
    evidence: dict | None = None,
) -> dict:
    now = now or dt.datetime.now(dt.UTC)
    sess = label_instant(now, session_provider)
    staleness = None
    if data_last_updated_at is not None:
        staleness = max(0.0, (now - data_last_updated_at).total_seconds())
    env = {
        "generated_at": now.isoformat(),
        "data_last_updated_at": data_last_updated_at.isoformat() if data_last_updated_at else None,
        "staleness_s": staleness,
        "warnings": warnings or [],
        "current_session": sess.session_primary,
        "session_overlap": sess.session_overlap,
        "session_integrity": sess.session_integrity,
        "session_model_version": sess.session_model_version,
        "data": data,
    }
    if evidence is not None:
        env["evidence"] = evidence
    return env


def envelope_is_valid(env: dict) -> bool:
    return REQUIRED_ENVELOPE_KEYS <= set(env.keys())
