"""Phase 6 — Wilson/BH references, fee math, maker fills, determinism, leakage,
session-scope planted edge, and the NULL-dataset FDR guard (most important test).
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
from sqlalchemy import select

from pmre.analytics.calibration import (
    SnapshotObs,
    build_calibration_bins,
    build_timestamp_performance,
    price_bin,
)
from pmre.analytics.ev import net_ev_maker, net_ev_taker
from pmre.analytics.maker_fill import MakerFillModel, MakerPost, Print, did_fill
from pmre.analytics.regime import RegimeLabeler
from pmre.analytics.runner import HourlyAnalytics
from pmre.analytics.stats import benjamini_hochberg, brier_score, log_loss, wilson_interval
from pmre.analytics.walkforward import DatedObs, WalkForward, rolling_windows
from pmre.db.models import CalibrationBin, Market, MarketResolution, Snapshot
from pmre.feecurve import taker_fee_per_share


# --- Wilson CI vs independent closed form --------------------------------
def _wilson_reference(wins, n, z=1.959963984540054):
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return center - half, center + half


def test_wilson_matches_closed_form():
    for wins, n in [(8, 10), (50, 100), (490, 500), (1, 20)]:
        lo, hi = wilson_interval(wins, n)
        rlo, rhi = _wilson_reference(wins, n)
        assert abs(lo - rlo) < 1e-6
        assert abs(hi - rhi) < 1e-6


# --- BH-FDR vs hand-computed ---------------------------------------------
def test_benjamini_hochberg_hand_computed():
    pvals = [0.001, 0.008, 0.039, 0.041, 0.9]
    # BH q=0.10: largest k with p_(k) <= k*q/m is k=4 → reject first four.
    assert benjamini_hochberg(pvals, q=0.10) == [True, True, True, True, False]


def test_benjamini_hochberg_all_null_rejects_none():
    pvals = [0.4, 0.5, 0.6, 0.7, 0.99]
    assert benjamini_hochberg(pvals, q=0.10) == [False] * 5


# --- fee math -------------------------------------------------------------
def test_fee_symmetric_and_zero_at_bounds():
    assert taker_fee_per_share(0.0) == 0.0
    assert taker_fee_per_share(1.0) == 0.0
    for d in (0.1, 0.2, 0.3):
        assert abs(taker_fee_per_share(0.5 - d) - taker_fee_per_share(0.5 + d)) < 1e-12
    # peak at 0.5
    assert taker_fee_per_share(0.5) > taker_fee_per_share(0.6)


def test_net_ev_formulas():
    # win_rate 0.6, entry 0.55 → gross 0.05, minus fee at 0.55
    ev = net_ev_taker(0.6, 0.55, fee_rate=0.072)
    assert abs(ev - (0.05 - taker_fee_per_share(0.55, 0.072))) < 1e-12
    # maker: p_fill scales gross, no fee
    assert abs(net_ev_maker(0.6, 0.55, p_fill=0.5) - 0.5 * 0.05) < 1e-12
    assert net_ev_maker(0.6, 0.55, p_fill=0.0) == 0.0


def test_brier_and_logloss():
    # perfect forecasts
    assert brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert log_loss([1.0, 0.0], [1, 0]) < 1e-6
    # worst-ish
    assert brier_score([0.0, 1.0], [1, 0]) == 1.0


# --- maker fill model -----------------------------------------------------
def test_did_fill_truth():
    post = MakerPost("t_240", "mid", price=0.60, ts=0.0, token_id="U")
    prints = [Print(ts=5.0, price=0.59, token_id="U"), Print(ts=8.0, price=0.58, token_id="U")]
    filled, ttf = did_fill(post, prints)
    assert filled and ttf == 5.0
    # a post below all prints never fills
    post_low = MakerPost("t_240", "join_bid", price=0.55, ts=0.0, token_id="U")
    filled2, ttf2 = did_fill(post_low, prints)
    assert not filled2 and ttf2 is None


def test_maker_fill_model_aggregate():
    posts = [
        MakerPost("t_240", "mid", 0.60, 0.0, token_id="U"),
        MakerPost("t_240", "mid", 0.60, 100.0, token_id="U"),
    ]
    prints = [Print(5.0, 0.59, "U")]  # only the first post fills
    ests = MakerFillModel(horizon_s=50.0).estimate(posts, prints)
    e = [x for x in ests if x.label == "t_240" and x.post_style == "mid"][0]
    assert e.sample_size == 2
    assert e.p_fill == 0.5
    assert e.median_ttf_s == 5.0


# --- regime labeler -------------------------------------------------------
def test_regime_quantiles():
    rl = RegimeLabeler().fit([0.0001 * i for i in range(1, 100)])
    assert rl.label(0.00005) == "calm"
    assert rl.label(0.0099) == "volatile"
    assert rl.label(0.005) == "normal"


# --- walk-forward + leakage ----------------------------------------------
def test_rolling_windows_shape():
    ws = rolling_windows(dt.date(2026, 1, 1), dt.date(2026, 2, 1), 14, 3, 3)
    assert ws
    for w in ws:
        assert (w.train_end - w.train_start).days == 14
        assert (w.val_end - w.val_start).days == 3
        assert w.val_start == w.train_end  # contiguous, no overlap


def test_walkforward_leakage_zero_shared_market_ids():
    obs = []
    mid = 0
    base = dt.date(2026, 1, 1)
    for day in range(30):
        d = base + dt.timedelta(days=day)
        for _ in range(20):
            mid += 1
            obs.append(DatedObs(market_id=mid, date=d, won=1, entry_price=0.50))
    res = WalkForward().evaluate(obs)
    assert res.windows
    for w in res.windows:
        assert w.train_market_ids.isdisjoint(w.val_market_ids)


def test_walkforward_detects_persistent_positive_edge():
    # strong edge: win rate 0.75 at entry 0.55 → CI-lower net EV > 0 every window
    obs = []
    base = dt.date(2026, 1, 1)
    rng = np.random.default_rng(0)
    mid = 0
    for day in range(40):
        d = base + dt.timedelta(days=day)
        for _ in range(80):
            mid += 1
            won = int(rng.random() < 0.75)
            obs.append(DatedObs(market_id=mid, date=d, won=won, entry_price=0.55))
    res = WalkForward().evaluate(obs)
    assert res.passed
    assert res.max_consecutive_positive >= 2


# --- session-scope planted edge ------------------------------------------
def _obs(mid, won, session="new_york", integrity="regular"):
    return SnapshotObs(
        label="t_240", dominant_side="UP", dominant_mid=mid, dominant_ask=mid + 0.01,
        dominant_bid=mid - 0.01, won=won, session_primary=session,
        session_integrity=integrity,
    )


def test_planted_ny_edge_detected_and_holiday_excluded():
    rng = np.random.default_rng(42)
    obs = []
    # NY regular: planted +0.10 edge in bin around 0.60
    for _ in range(600):
        obs.append(_obs(0.60, int(rng.random() < 0.70)))  # win 0.70 vs price 0.60
    # London regular: calibrated (win ~ price)
    for _ in range(600):
        obs.append(_obs(0.60, int(rng.random() < 0.60), session="london"))
    # NY holiday: different behaviour, must NOT contaminate regular NY bucket
    for _ in range(300):
        obs.append(_obs(0.60, int(rng.random() < 0.40), session="new_york", integrity="holiday"))

    bins = build_calibration_bins(obs, q=0.10, fdr_min_n=50)
    by = {(b.scope, b.session_integrity_filter): b for b in bins
          if abs(b.price_bin_lo - 0.60) < 1e-9}

    ny_reg = by[("session:new_york", "regular")]
    assert ny_reg.n == 600  # holiday rows excluded
    assert ny_reg.edge > 0.05
    assert ny_reg.fdr_pass is True

    # holiday rows are in their own bucket
    ny_hol = by[("session:new_york", "holiday")]
    assert ny_hol.n == 300
    assert ny_hol.win_rate < 0.55

    # total scope edge is diluted vs the NY-only edge
    total = by[("total", "regular")]
    assert total.edge < ny_reg.edge


# --- determinism ----------------------------------------------------------
def _seed_resolved_snapshots(db, n_per_bin=120, seed=1):
    rng = np.random.default_rng(seed)
    with db.session() as s:
        for k, mid_price in enumerate([0.56, 0.62, 0.68]):
            for i in range(n_per_bin):
                m = Market(slug=f"btc-updown-5m-{k}-{i}", fee_rate_bps=72.0)
                s.add(m)
                s.flush()
                won = int(rng.random() < mid_price)  # calibrated
                s.add(MarketResolution(market_id=m.id, winning_outcome="UP" if won else "DOWN"))
                s.add(Snapshot(
                    market_id=m.id, label="t_240", target_seconds_left=240,
                    captured_at=dt.datetime(2026, 7, 7, 18, 1, tzinfo=dt.UTC),
                    dominant_side="UP", dominant_mid=mid_price, dominant_ask=mid_price + 0.01,
                    up_best_bid=mid_price - 0.01, down_best_bid=0.30,
                    session_primary="new_york", session_integrity="regular",
                    was_correct_mid=bool(won),
                ))
        s.commit()


def test_hourly_determinism_same_hash(db):
    _seed_resolved_snapshots(db)
    runner = HourlyAnalytics(db.session_factory)
    rid1 = runner.run(run_id="run-a")
    rid2 = runner.run(run_id="run-b")
    with db.session() as s:
        from pmre.db.models import AnalysisRun
        r1 = s.execute(select(AnalysisRun).where(AnalysisRun.run_id == rid1)).scalar_one()
        r2 = s.execute(select(AnalysisRun).where(AnalysisRun.run_id == rid2)).scalar_one()
        assert r1.summary_hash == r2.summary_hash
        # calibration bins were written
        bins = s.execute(select(CalibrationBin).where(CalibrationBin.run_id == rid1)).scalars().all()
        assert len(bins) > 0


# --- THE null-dataset FDR guard ------------------------------------------
def _null_run_passes(rng, n_bins=12, n_per_bin=300):
    """One perfectly-calibrated dataset → number of FDR-passing bins."""
    obs = []
    for b in range(n_bins):
        lo = 0.50 + b * 0.02
        for _ in range(n_per_bin):
            mid = lo + rng.random() * 0.02
            if mid >= 0.98:
                mid = 0.979
            won = int(rng.random() < mid)  # outcome prob == price → calibrated
            obs.append(SnapshotObs(
                label="t_240", dominant_side="UP", dominant_mid=mid,
                dominant_ask=mid + 0.005, dominant_bid=mid - 0.005, won=won,
                session_primary=None,
            ))
    bins = build_calibration_bins(obs, q=0.10, fdr_min_n=50)
    return sum(1 for x in bins if x.fdr_pass), len(bins)


def test_null_dataset_produces_almost_no_fdr_passes():
    rng = np.random.default_rng(2024)
    runs = 100
    total_pass = 0
    runs_with_pass = 0
    total_bins = 0
    for _ in range(runs):
        passes, nbins = _null_run_passes(rng)
        total_pass += passes
        total_bins += nbins
        runs_with_pass += 1 if passes > 0 else 0
    # BH controls FDR at q=0.10 → under the complete null P(any rejection) is small.
    # A naive per-bin alpha=0.05 would flag ~0.05 * total_bins ≈ 60 bins here.
    assert runs_with_pass <= 20, f"too many runs with false discovery: {runs_with_pass}"
    assert total_pass <= 25, f"too many total false discoveries: {total_pass}"
    # sanity: the corrected pass rate is far below the naive 5% level
    assert total_pass / total_bins < 0.02


def test_planted_5c_edge_survives_fdr_amid_null_bins():
    rng = np.random.default_rng(7)
    obs = []
    # 11 calibrated null bins
    for b in range(11):
        lo = 0.50 + b * 0.02
        for _ in range(300):
            mid = lo + rng.random() * 0.02
            obs.append(SnapshotObs("t_240", "UP", mid, mid + 0.005, mid - 0.005,
                                   int(rng.random() < mid), session_primary=None))
    # 1 planted bin around 0.72 with +0.05 edge, large n
    for _ in range(600):
        mid = 0.72 + rng.random() * 0.02
        obs.append(SnapshotObs("t_240", "UP", mid, mid + 0.005, mid - 0.005,
                               int(rng.random() < mid + 0.05), session_primary=None))
    bins = build_calibration_bins(obs, q=0.10, fdr_min_n=50)
    planted = [b for b in bins if abs(b.price_bin_lo - 0.72) < 1e-9][0]
    assert planted.fdr_pass is True
    assert planted.edge > 0.02


def test_timestamp_performance_has_both_directions_and_scopes():
    obs = [_obs(0.60, 1) for _ in range(300)] + [_obs(0.60, 0) for _ in range(200)]
    perf = build_timestamp_performance(obs, fdr_min_n=50)
    styles = {(p.entry_style, p.direction) for p in perf}
    assert ("taker_ask", "dominant") in styles
    assert ("taker_ask", "contrarian") in styles
    scopes = {p.scope for p in perf}
    assert "total" in scopes and "session:new_york" in scopes


def test_overlap_credits_both_open_sessions():
    # During the London/NY overlap the snapshot is stamped session_primary=new_york
    # (NY has the higher priority) with session_overlap=london_ny_overlap. London
    # was still open, so it must get per-session credit — otherwise the London
    # bucket silently loses its most active window to new_york.
    obs = [
        SnapshotObs(
            "t_240", "UP", 0.60, 0.61, 0.59, won=1,
            session_primary="new_york", session_overlap="london_ny_overlap",
        )
        for _ in range(300)
    ]
    perf = build_timestamp_performance(obs, fdr_min_n=50)
    scopes = {p.scope for p in perf}
    assert "session:new_york" in scopes
    assert "session:london" in scopes  # the fix: London is credited for the overlap
    assert "overlap:london_ny_overlap" in scopes
    # Both single-session scopes see the full population (each obs had both open).
    ny_n = next(p.n for p in perf if p.scope == "session:new_york"
                and p.entry_style == "taker_ask" and p.direction == "dominant")
    ldn_n = next(p.n for p in perf if p.scope == "session:london"
                 and p.entry_style == "taker_ask" and p.direction == "dominant")
    assert ny_n == ldn_n == 300


def test_price_bin_edges():
    assert price_bin(0.50) == (0.50, 0.52)
    assert price_bin(0.519) == (0.50, 0.52)
    assert price_bin(0.52) == (0.52, 0.54)
    assert price_bin(0.49) is None
    assert price_bin(0.98) is None
