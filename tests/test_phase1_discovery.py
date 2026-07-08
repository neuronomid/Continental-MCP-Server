"""Phase 1 — slug math, Gamma/CLOB parsing, ambiguous refusal, fee params."""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select

from pmre.collectors.discovery import (
    AmbiguousMappingError,
    DiscoveryService,
    parse_clob_market_info,
    parse_gamma_market,
)
from pmre.collectors.slugs import (
    PERIOD_S,
    expected_windows,
    parse_slug_start,
    slug_for,
    window_start_unix,
)
from pmre.db.models import FeeSchedule, Market, MarketToken, SystemHealthEvent
from tests.helpers import load_json_fixture


def load_fixture(name: str) -> dict:
    return load_json_fixture(f"raw/{name}")


# --- slug math ------------------------------------------------------------
@given(st.integers(min_value=1_600_000_000, max_value=2_000_000_000))
def test_window_start_is_aligned_and_contains_instant(ts):
    start = window_start_unix(ts)
    assert start % PERIOD_S == 0
    assert start <= ts < start + PERIOD_S


@given(st.integers(min_value=1_600_000_000, max_value=2_000_000_000))
def test_slug_roundtrip(ts):
    start = window_start_unix(ts)
    assert parse_slug_start(slug_for(start)) == start


def test_expected_windows_are_sequential_and_current_first():
    now = dt.datetime(2026, 7, 7, 18, 2, 33, tzinfo=dt.UTC)
    wins = expected_windows(now, 4)
    starts = [u for u, _ in wins]
    assert starts[0] == window_start_unix(now)
    assert starts == [starts[0] + i * PERIOD_S for i in range(4)]
    assert all(s.startswith("btc-updown-5m-") for _, s in wins)


def test_et_alignment_matches_utc_five_minute_boundary():
    # A 300s-aligned unix start lands on a wall-clock minute divisible by 5.
    now = dt.datetime(2026, 3, 8, 14, 37, 12, tzinfo=dt.UTC)  # near US DST
    start = window_start_unix(now)
    d = dt.datetime.fromtimestamp(start, tz=dt.UTC)
    assert d.minute % 5 == 0 and d.second == 0


# --- Gamma parsing --------------------------------------------------------
def test_parse_gamma_market_golden():
    raw = load_fixture("gamma_btc5m_market.json")
    m = parse_gamma_market(raw)
    assert m.slug == "btc-updown-5m-1783447200"
    assert m.condition_id == "0xabc123condition"
    assert m.enable_order_book is True
    assert m.price_to_beat == 108250.5
    assert m.price_to_beat_source == "gamma"
    assert m.slug_derived_start_utc == dt.datetime(2026, 7, 7, 18, 0, tzinfo=dt.UTC)
    assert m.expected_resolution_time_utc == dt.datetime(2026, 7, 7, 18, 5, tzinfo=dt.UTC)
    up = [t for t in m.tokens if t.outcome == "UP"][0]
    dn = [t for t in m.tokens if t.outcome == "DOWN"][0]
    assert up.token_id.startswith("71321045")
    assert dn.token_id.startswith("52114319")
    assert up.outcome_index == 0 and dn.outcome_index == 1


def test_parse_gamma_ambiguous_refused():
    raw = load_fixture("gamma_btc5m_ambiguous.json")
    with pytest.raises(AmbiguousMappingError):
        parse_gamma_market(raw)


def test_parse_gamma_length_mismatch_refused():
    raw = {"slug": "btc-updown-5m-1", "outcomes": "[\"Up\",\"Down\"]", "clobTokenIds": "[\"1\"]"}
    with pytest.raises(AmbiguousMappingError):
        parse_gamma_market(raw)


# --- CLOB fee parsing -----------------------------------------------------
def test_parse_clob_fees_on():
    fp = parse_clob_market_info(load_fixture("clob_market_info_fees_on.json"))
    assert fp.fees_enabled is True
    assert fp.fee_rate_bps == 72.0
    assert fp.maker_rebate_bps == 0.0
    assert fp.tick_size == 0.001
    assert fp.min_order_size == 5.0


def test_parse_clob_fees_off():
    fp = parse_clob_market_info(load_fixture("clob_market_info_fees_off.json"))
    assert fp.fees_enabled is False
    assert fp.fee_rate_bps == 0.0
    assert fp.tick_size == 0.01


# --- persistence ----------------------------------------------------------
def test_discovery_service_persists_market_tokens_and_fee_history(db):
    parsed = parse_gamma_market(load_fixture("gamma_btc5m_market.json"))
    fees = parse_clob_market_info(load_fixture("clob_market_info_fees_on.json"))
    svc = DiscoveryService(db.session_factory)
    mid = svc.upsert_market(parsed, fees)
    # re-run (idempotent upsert) → still one market, fee history grows
    svc.upsert_market(parsed, fees)
    with db.session() as s:
        markets = s.execute(select(Market)).scalars().all()
        assert len(markets) == 1
        assert markets[0].id == mid
        assert markets[0].fees_enabled is True
        assert markets[0].tick_size == 0.001
        tokens = s.execute(select(MarketToken)).scalars().all()
        assert len(tokens) == 2
        fee_rows = s.execute(select(FeeSchedule)).scalars().all()
        assert len(fee_rows) == 2  # history preserved across captures


def test_discovery_ambiguous_writes_health_event(db):
    from pmre.ops.health import HealthMonitor

    hm = HealthMonitor(db.session_factory)
    svc = DiscoveryService(db.session_factory, health=hm)
    raw = load_fixture("gamma_btc5m_ambiguous.json")
    try:
        parse_gamma_market(raw)
    except AmbiguousMappingError as e:
        svc.handle_ambiguous(raw["slug"], e)
    with db.session() as s:
        ev = s.execute(
            select(SystemHealthEvent).where(SystemHealthEvent.service == "discovery")
        ).scalar_one()
        assert "ambiguous" in ev.message
