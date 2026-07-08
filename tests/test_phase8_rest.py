"""Phase 8 — auth matrix, idempotent ingest, freshness envelope, contract."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from pmre.config import Settings
from pmre.db.models import Market, MarketToken, PaperTrade, Snapshot, StrategyCandidate
from pmre.serving.auth import create_ingest_token
from pmre.serving.envelope import REQUIRED_ENVELOPE_KEYS
from pmre.serving.rest import create_app


@pytest.fixture
def app_ctx(db):
    settings = Settings(
        env="dev", rest_bearer_tokens="read-tok", mcp_bearer_token="mcp-tok",
        ingest_bearer_token="ignored",
    )
    # seed a market + snapshot + candidate
    with db.session() as s:
        m = Market(slug="btc-updown-5m-1783447200", price_to_beat=108000.0, fee_rate_bps=72.0,
                   fees_enabled=True, tick_size=0.001,
                   expected_resolution_time_utc=dt.datetime(2099, 1, 1, tzinfo=dt.UTC))
        s.add(m)
        s.flush()
        s.add_all([
            MarketToken(market_id=m.id, token_id="U", outcome="UP"),
            MarketToken(market_id=m.id, token_id="D", outcome="DOWN"),
        ])
        s.add(Snapshot(market_id=m.id, label="t_240", target_seconds_left=240,
                       captured_at=dt.datetime.now(dt.UTC), dominant_side="UP",
                       dominant_mid=0.61, p_fair=0.60, model_edge=0.01,
                       session_primary="new_york", session_integrity="regular"))
        s.add(StrategyCandidate(candidate_id="champ-1", status="champion", label="t_240",
                                entry_style="taker_ask", net_ev_ci_lower_95=0.007))
        s.commit()
        mid = m.id
    ingest_token = create_ingest_token(db.session_factory, label="bot")
    app = create_app(db.session_factory, settings)
    return TestClient(app), ingest_token, mid


def _read(h):
    return {"Authorization": f"Bearer {h}"}


# --- auth matrix ----------------------------------------------------------
def test_read_requires_valid_token(app_ctx):
    client, _, _ = app_ctx
    assert client.get("/v1/session/current").status_code == 401
    assert client.get("/v1/session/current", headers=_read("wrong")).status_code == 401
    assert client.get("/v1/session/current", headers=_read("read-tok")).status_code == 200


def test_health_is_public(app_ctx):
    client, _, _ = app_ctx
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert REQUIRED_ENVELOPE_KEYS <= set(r.json().keys())


def test_ingest_rejects_read_token_and_accepts_ingest_token(app_ctx):
    client, ingest_token, mid = app_ctx
    rec = {"client_uuid": "u-1", "candidate_id": "champ-1", "pnl": 1.0}
    # read token cannot ingest
    assert client.post("/v1/ingest/paper-trades", json=rec, headers=_read("read-tok")).status_code == 401
    # ingest token works
    r = client.post("/v1/ingest/paper-trades", json=rec, headers=_read(ingest_token))
    assert r.status_code == 200
    assert r.json()["created"] is True


# --- envelope / freshness -------------------------------------------------
def test_envelope_present_on_read(app_ctx):
    client, _, _ = app_ctx
    r = client.get("/v1/session/current", headers=_read("read-tok"))
    body = r.json()
    assert REQUIRED_ENVELOPE_KEYS <= set(body.keys())
    assert body["current_session"] is not None
    assert body["session_model_version"].startswith("sessions-")
    assert "staleness_s" in body


def test_markets_current_returns_seeded_market(app_ctx):
    client, _, mid = app_ctx
    r = client.get("/v1/markets/current", headers=_read("read-tok"))
    data = r.json()["data"]
    assert data["active"]["market_id"] == mid
    assert data["active"]["price_to_beat"] == 108000.0
    assert {t["outcome"] for t in data["active"]["tokens"]} == {"UP", "DOWN"}


def test_champion_endpoint(app_ctx):
    client, _, _ = app_ctx
    r = client.get("/v1/candidates/champion", headers=_read("read-tok"))
    data = r.json()["data"]
    assert data["candidate_id"] == "champ-1"
    assert data["net_ev_ci_lower_95"] == 0.007


# --- idempotent ingest ----------------------------------------------------
def test_ingest_idempotent_on_uuid(app_ctx, db):
    client, ingest_token, _ = app_ctx
    rec = {"client_uuid": "dup-1", "pnl": 2.5}
    r1 = client.post("/v1/ingest/paper-trades", json=rec, headers=_read(ingest_token))
    r2 = client.post("/v1/ingest/paper-trades", json=rec, headers=_read(ingest_token))
    assert r1.json()["created"] is True
    assert r2.status_code == 200
    assert r2.json()["created"] is False  # no duplicate
    with db.session() as s:
        from sqlalchemy import select
        rows = s.execute(select(PaperTrade).where(PaperTrade.client_uuid == "dup-1")).scalars().all()
        assert len(rows) == 1


def test_ingest_rejects_unknown_fields(app_ctx):
    client, ingest_token, _ = app_ctx
    rec = {"client_uuid": "u-x", "pnl": 1.0, "surprise_field": "boom"}
    r = client.post("/v1/ingest/paper-trades", json=rec, headers=_read(ingest_token))
    assert r.status_code == 422  # pydantic extra=forbid


def test_ingest_backfill_many(app_ctx, db):
    client, ingest_token, _ = app_ctx
    for i in range(200):
        client.post("/v1/ingest/bot-heartbeat",
                    json={"client_uuid": f"hb-{i}", "bot_id": "bot-a", "bot_status": "running"},
                    headers=_read(ingest_token))
    from sqlalchemy import func, select

    from pmre.db.models import BotHeartbeat
    with db.session() as s:
        n = s.execute(select(func.count()).select_from(BotHeartbeat)).scalar_one()
    assert n == 200


# --- OpenAPI contract -----------------------------------------------------
def test_openapi_published_with_expected_paths(app_ctx):
    client, _, _ = app_ctx
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    for expected in [
        "/v1/health", "/v1/session/current", "/v1/markets/current",
        "/v1/performance/timestamps", "/v1/performance/calibration",
        "/v1/candidates/champion", "/v1/ingest/paper-trades",
    ]:
        assert expected in paths
