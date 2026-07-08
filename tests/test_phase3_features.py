"""Phase 3 — fair value sanity, BTC features on synthetic ticks, divergence."""

from __future__ import annotations

import math

from sqlalchemy import select

from pmre.collectors.btc_feed import BtcFeed, OneSecondBarAggregator
from pmre.db.models import BtcTick
from pmre.features.btc_state import BtcFeatureState, divergence_bps
from pmre.features.fair_value import compute_z, model_edge, p_fair, p_fair_from_z


# --- fair value sanity ----------------------------------------------------
def test_pfair_z_zero_is_half():
    # S == S_ptb → z = 0 → 0.5
    assert abs(p_fair(100.0, 100.0, 0.001, 100.0) - 0.5) < 1e-9
    assert abs(p_fair_from_z(0.0) - 0.5) < 1e-12


def test_pfair_large_positive_z_to_one():
    # far above price-to-beat with little time/vol → ~1
    val = p_fair(110000.0, 100000.0, 0.0001, 5.0)
    assert val > 0.999


def test_pfair_large_negative_z_to_zero():
    val = p_fair(90000.0, 100000.0, 0.0001, 5.0)
    assert val < 0.001


def test_pfair_tau_zero_deterministic_and_clamped():
    assert p_fair(101.0, 100.0, 0.001, 0.0) == 1.0
    assert p_fair(99.0, 100.0, 0.001, 0.0) == 0.0
    assert p_fair(100.0, 100.0, 0.001, 0.0) == 0.5
    # tiny tau doesn't explode into nan/inf
    v = p_fair(100.5, 100.0, 1e-9, 1e-6)
    assert 0.0 <= v <= 1.0 and not math.isnan(v)


def test_sigma_floor_prevents_explosion():
    # sigma below floor is clamped → z finite
    z = compute_z(100.5, 100.0, sigma_1s=0.0, tau_s=10.0, sigma_floor=1e-6)
    assert math.isfinite(z)


def test_pfair_monotone_in_price():
    lo = p_fair(100.2, 100.0, 0.002, 60.0)
    hi = p_fair(100.8, 100.0, 0.002, 60.0)
    assert hi > lo


def test_model_edge_sign():
    # market says UP 0.70, fair 0.5 → market rich (+)
    e = model_edge(0.70, 100.0, 100.0, 0.001, 60.0)
    assert e > 0


# --- divergence -----------------------------------------------------------
def test_divergence_bps_flag():
    # 108000 vs 108108 → ~10 bps
    d = divergence_bps(108000.0, 108108.0)
    assert 9.5 < d < 10.5
    assert divergence_bps(None, 1.0) is None


# --- BTC feature state on synthetic ticks ---------------------------------
def test_returns_on_known_sequence():
    st = BtcFeatureState(min_samples=3)
    base = 1_000_000.0
    # price grows by exactly factor exp(0.001) each second for 61s
    price = 100000.0
    for i in range(61):
        st.update(base + i, price)
        price *= math.exp(0.001)
    # ret over 5s = 5 * 0.001 = 0.005
    assert abs(st.ret(5) - 0.005) < 1e-6
    assert abs(st.ret(60) - 0.060) < 1e-6
    assert st.has_enough_history()


def test_ewma_sigma_on_constant_squared_returns():
    st = BtcFeatureState(ewma_lambda=0.94, sigma_floor=1e-9)
    base = 0.0
    price = 100.0
    # alternate up/down by fixed log-return magnitude r → EWMA var → r^2
    r = 0.002
    for i in range(200):
        price *= math.exp(r if i % 2 == 0 else -r)
        st.update(base + i, price)
    # EWMA variance converges to r^2 → sigma ≈ r
    assert abs(st.sigma_1s() - r) < 1e-4


def test_insufficient_history_marks_degraded():
    st = BtcFeatureState(min_samples=10)
    st.update(0.0, 100000.0)
    st.update(1.0, 100010.0)
    bs = st.build_btc_state(price_to_beat=100000.0, tau_s=120.0)
    assert bs.quality == "degraded"
    assert bs.p_fair is not None  # still computed, just flagged


def test_no_data_is_missing():
    st = BtcFeatureState()
    bs = st.build_btc_state(price_to_beat=100000.0, tau_s=120.0)
    assert bs.quality == "missing"
    assert bs.price is None


def test_build_btc_state_distance_and_divergence():
    st = BtcFeatureState(min_samples=2)
    st.update(0.0, 100000.0)
    st.update(1.0, 100500.0)
    bs = st.build_btc_state(price_to_beat=100000.0, tau_s=120.0, secondary_price=100450.0)
    assert abs(bs.distance_from_start - 500.0) < 1e-6
    assert abs(bs.distance_bps - 50.0) < 1e-3  # 0.5% = 50 bps
    assert bs.divergence_bps is not None


# --- 1s bar aggregation ---------------------------------------------------
def test_one_second_bar_rollover():
    agg = OneSecondBarAggregator()
    assert agg.add(100.2, 50.0, 1.0) is None
    assert agg.add(100.7, 51.0, 2.0) is None  # same second
    bar = agg.add(101.1, 49.0, 1.0)  # rolls to next second → completes second 100
    assert bar is not None
    assert bar["open"] == 50.0
    assert bar["high"] == 51.0
    assert bar["low"] == 50.0
    assert bar["close"] == 51.0
    assert bar["volume"] == 3.0
    assert bar["trade_count"] == 2


def test_btc_feed_persists_bars(db):
    feed = BtcFeed(session_factory=db.session_factory)
    # two ticks in second 100, one in second 101 → completes second 100 bar
    feed.ingest_tick("binance_spot", 100.1, 50000.0, 0.5)
    feed.ingest_tick("binance_spot", 100.9, 50010.0, 0.3)
    feed.ingest_tick("binance_spot", 101.2, 50020.0, 0.1)
    with db.session() as s:
        bars = s.execute(select(BtcTick)).scalars().all()
        assert len(bars) == 1
        assert bars[0].source == "binance_spot"
        assert bars[0].open == 50000.0


def test_parse_binance_and_coinbase():
    p = BtcFeed.parse_binance_trade({"e": "aggTrade", "T": 1783447000000, "p": "108000.5", "q": "0.01"})
    assert p == (1783447000.0, 108000.5, 0.01)
    c = BtcFeed.parse_coinbase_match({"type": "match", "price": "108010.0", "size": "0.02", "time": "2026-07-07T18:00:00Z"})
    assert c[1] == 108010.0
