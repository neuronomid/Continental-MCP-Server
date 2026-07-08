"""Phase 0 — config fail-fast, schema round-trip, migrations, ops/health, alerts."""

from __future__ import annotations

import datetime as dt

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from pmre.config import ConfigError, Settings, load_settings
from pmre.db.models import Market, MarketToken, Snapshot, SystemHealthEvent
from pmre.ops.alerts import AlertLevel, TelegramAlerter, format_alert
from pmre.ops.clock import evaluate_drift, parse_chronyc_tracking
from pmre.ops.health import HealthMonitor


# --- config ---------------------------------------------------------------
def test_dev_settings_boot_with_defaults():
    s = Settings(env="dev")
    assert s.database_url.startswith("sqlite")
    assert s.market_period_s == 300
    assert len(s.snapshot_offsets_s) == 9


def test_production_fails_fast_on_missing_secrets(monkeypatch):
    # No secrets set → production load must raise a *clear* error naming them.
    for var in [
        "PMRE_REST_BEARER_TOKENS",
        "PMRE_MCP_BEARER_TOKEN",
        "PMRE_INGEST_BEARER_TOKEN",
        "PMRE_ALERT_TELEGRAM_BOT_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError) as ei:
        load_settings(env="production")
    msg = str(ei.value)
    assert "PMRE_MCP_BEARER_TOKEN" in msg
    assert "PMRE_INGEST_BEARER_TOKEN" in msg


def test_production_ok_when_secrets_present():
    s = load_settings(
        env="production",
        database_url="postgresql+psycopg2://u:p@localhost/pmre",
        rest_bearer_tokens="a,b",
        mcp_bearer_token="m",
        ingest_bearer_token="i",
        alert_telegram_bot_token="bt",
        alert_telegram_chat_id="cid",
    )
    assert s.read_tokens == {"a", "b"}


def test_offsets_parse_from_csv_string():
    s = Settings(snapshot_offsets_s="270,240,30")
    assert s.snapshot_offsets_s == (270, 240, 30)


# --- schema round-trip ----------------------------------------------------
def test_market_and_token_round_trip(db):
    with db.session() as s:
        m = Market(
            slug="btc-updown-5m-1751900400",
            slug_derived_start_utc=dt.datetime(2025, 7, 7, 18, 0, tzinfo=dt.UTC),
            fees_enabled=True,
            fee_rate_bps=72.0,
            tick_size=0.001,
            price_to_beat=108000.0,
            price_to_beat_source="gamma",
        )
        s.add(m)
        s.flush()
        s.add_all(
            [
                MarketToken(market_id=m.id, token_id="tokUP", outcome="UP", outcome_index=0),
                MarketToken(market_id=m.id, token_id="tokDN", outcome="DOWN", outcome_index=1),
            ]
        )
    with db.session() as s:
        m2 = s.execute(select(Market).where(Market.slug == "btc-updown-5m-1751900400")).scalar_one()
        assert m2.fees_enabled is True
        assert m2.price_to_beat == 108000.0
        # tz-aware round trip
        assert m2.slug_derived_start_utc.tzinfo is not None
        assert len(m2.tokens) == 2
        assert {t.outcome for t in m2.tokens} == {"UP", "DOWN"}


def test_snapshot_round_trip_with_session_and_flags(db):
    with db.session() as s:
        m = Market(slug="btc-updown-5m-1751900700")
        s.add(m)
        s.flush()
        snap = Snapshot(
            market_id=m.id,
            label="t_240",
            target_seconds_left=240,
            snapshot_actual_seconds_left=239.987,
            captured_at=dt.datetime.now(dt.UTC),
            up_best_bid=0.60,
            up_best_ask=0.62,
            dominant_side="UP",
            dominant_mid=0.61,
            session_primary="new_york",
            session_integrity="regular",
            p_fair=0.58,
            model_edge=0.03,
            crossed_book_flag=False,
        )
        s.add(snap)
    with db.session() as s:
        got = s.execute(select(Snapshot)).scalar_one()
        assert got.label == "t_240"
        assert got.session_primary == "new_york"
        assert abs(got.snapshot_actual_seconds_left - 239.987) < 1e-6


def test_migration_upgrade_downgrade_idempotent(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command

    url = f"sqlite+pysqlite:///{tmp_path/'mig.db'}"
    monkeypatch.setenv("PMRE_ALEMBIC_URL", url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    # idempotent re-run of create (checkfirst) — upgrade again shouldn't error
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    # verify a core table exists
    from sqlalchemy import create_engine, inspect

    eng = create_engine(url)
    names = set(inspect(eng).get_table_names())
    assert {"markets", "snapshots", "calibration_bins", "session_calendar"} <= names


# --- ops: alerts ----------------------------------------------------------
def test_format_alert_deterministic():
    txt = format_alert(AlertLevel.CRITICAL, "clob_ws", "reconnect storm")
    assert txt == "🚨 [CRITICAL] clob_ws: reconnect storm"


@respx.mock
async def test_telegram_alert_posts_formatted_message():
    route = respx.post("https://api.telegram.org/botTESTTOKEN/sendMessage").mock(
        return_value=Response(200, json={"ok": True})
    )
    alerter = TelegramAlerter("TESTTOKEN", "12345")
    ok = await alerter.send(AlertLevel.WARNING, "snapshotter", "miss rate 3%")
    assert ok is True
    assert route.called
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["chat_id"] == "12345"
    assert body["text"] == "⚠️ [WARNING] snapshotter: miss rate 3%"


def test_alerter_disabled_when_no_token():
    alerter = TelegramAlerter("", "")
    assert alerter.enabled is False
    assert alerter.send_sync(AlertLevel.INFO, "svc", "hi") is False


# --- ops: health ----------------------------------------------------------
def test_health_heartbeat_and_silence_detection(db):
    hm = HealthMonitor(db.session_factory)
    hm.heartbeat("clob_ws")
    hm.heartbeat("btc_feed")
    now = dt.datetime.now(dt.UTC)
    # both fresh → none silent
    assert hm.silent_services(["clob_ws", "btc_feed"], 180, now) == []
    # a never-seen service is silent
    assert hm.silent_services(["snapshotter"], 180, now) == ["snapshotter"]
    # far-future 'now' → everything silent
    future = now + dt.timedelta(minutes=10)
    assert set(hm.silent_services(["clob_ws", "btc_feed"], 180, future)) == {
        "clob_ws",
        "btc_feed",
    }


def test_health_warning_writes_event_and_alerts(db):
    class FakeAlerter:
        def __init__(self):
            self.calls = []

        def send_sync(self, level, service, message):
            self.calls.append((level, service, message))
            return True

    fake = FakeAlerter()
    hm = HealthMonitor(db.session_factory, alerter=fake)
    hm.critical("disk", "disk > 85%", {"pct": 86})
    with db.session() as s:
        ev = s.execute(select(SystemHealthEvent)).scalar_one()
        assert ev.kind == "critical"
        assert ev.details_json["pct"] == 86
    assert fake.calls and fake.calls[0][1] == "disk"


# --- ops: clock -----------------------------------------------------------
def test_parse_chronyc_and_evaluate():
    sample = """Reference ID    : 0A0A0A0A (ntp)
Stratum         : 2
System time     : 0.000123456 seconds slow of NTP time
Last offset     : +0.000001 seconds
"""
    offset_ms = parse_chronyc_tracking(sample)
    assert abs(offset_ms - 0.123456) < 1e-6
    st = evaluate_drift(offset_ms, abort_ms=250, warn_ms=50)
    assert st.ok and not st.warn


def test_evaluate_drift_abort_threshold():
    st = evaluate_drift(300.0, abort_ms=250, warn_ms=50)
    assert not st.ok and st.warn
