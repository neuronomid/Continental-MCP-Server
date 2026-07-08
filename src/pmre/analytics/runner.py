"""Orchestration of hourly/daily analytics runs → DB tables + run summary.

Determinism: identical inputs + identical model versions ⇒ identical
``summary_hash`` (mcp_phases.md Phase 6 determinism test).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import orjson
from sqlalchemy import select

from .. import FEATURE_VERSION, REGIME_MODEL_VERSION
from ..config import Settings
from ..db.models import (
    AnalysisRun,
    CalibrationBin,
    Market,
    MarketResolution,
    Snapshot,
    TimestampPerformance,
)
from .calibration import (
    SnapshotObs,
    build_calibration_bins,
    build_timestamp_performance,
)


def load_obs(
    session_factory, window_start: dt.datetime | None = None, window_end: dt.datetime | None = None
) -> list[SnapshotObs]:
    """Load resolved-market snapshots as :class:`SnapshotObs`."""
    obs: list[SnapshotObs] = []
    with session_factory() as s:
        resolutions = {r.market_id: r for r in s.execute(select(MarketResolution)).scalars()}
        markets = {m.id: m for m in s.execute(select(Market)).scalars()}
        q = select(Snapshot)
        if window_start is not None:
            q = q.where(Snapshot.captured_at >= window_start)
        if window_end is not None:
            q = q.where(Snapshot.captured_at < window_end)
        for snap in s.execute(q).scalars():
            r = resolutions.get(snap.market_id)
            if r is None or snap.was_correct_mid is None:
                continue  # only resolved + labeled snapshots
            m = markets.get(snap.market_id)
            fee_rate = 0.072
            if m and m.fee_rate_bps:
                fee_rate = m.fee_rate_bps / 1000.0
            dom_bid = None
            if snap.dominant_side == "UP":
                dom_bid = snap.up_best_bid
            elif snap.dominant_side == "DOWN":
                dom_bid = snap.down_best_bid
            obs.append(
                SnapshotObs(
                    label=snap.label,
                    dominant_side=snap.dominant_side,
                    dominant_mid=snap.dominant_mid,
                    dominant_ask=snap.dominant_ask,
                    dominant_bid=dom_bid,
                    won=1 if snap.was_correct_mid else 0,
                    session_primary=snap.session_primary,
                    session_overlap=snap.session_overlap,
                    session_integrity=snap.session_integrity or "regular",
                    regime=snap.regime,
                    fee_rate=fee_rate,
                    max_usd=snap.max_usd_buy_within_2c,
                    was_close_call=bool(snap.was_close_call),
                    quality_ok=not (snap.stale_book_flag or snap.crossed_book_flag or snap.bad_sum_flag),
                )
            )
    return obs


def _summary_hash(payload: dict) -> str:
    canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical).hexdigest()


class HourlyAnalytics:
    def __init__(self, session_factory, settings: Settings | None = None):
        self.session_factory = session_factory
        self.settings = settings or Settings()

    def run(
        self,
        window_start: dt.datetime | None = None,
        window_end: dt.datetime | None = None,
        run_id: str | None = None,
        fdr_min_n: int = 50,
    ) -> str:
        run_id = run_id or f"hourly-{uuid.uuid4().hex[:12]}"
        started = dt.datetime.now(dt.UTC)
        obs = load_obs(self.session_factory, window_start, window_end)

        bins = build_calibration_bins(obs, q=self.settings.fdr_q, fdr_min_n=fdr_min_n)
        perf = build_timestamp_performance(obs, q=self.settings.fdr_q, fdr_min_n=fdr_min_n)

        with self.session_factory() as s:
            for b in bins:
                s.add(
                    CalibrationBin(
                        run_id=run_id, label=b.label,
                        price_bin_lo=b.price_bin_lo, price_bin_hi=b.price_bin_hi,
                        scope=b.scope, session_integrity_filter=b.session_integrity_filter,
                        regime=b.regime, n=b.n, wins=b.wins, win_rate=b.win_rate,
                        avg_entry_price=b.avg_entry_price, wilson_lo=b.wilson_lo,
                        wilson_hi=b.wilson_hi, edge=b.edge, p_value=b.p_value,
                        fdr_pass=b.fdr_pass,
                    )
                )
            for p in perf:
                s.add(
                    TimestampPerformance(
                        run_id=run_id, label=p.label, scope=p.scope,
                        session_integrity_filter=p.session_integrity_filter,
                        entry_style=p.entry_style, direction=p.direction, regime=p.regime,
                        n=p.n, win_rate=p.win_rate, avg_price=p.avg_price,
                        edge_vs_price=p.edge_vs_price, net_ev_taker=p.net_ev_taker,
                        net_ev_maker=p.net_ev_maker, net_ev_ci_lower_95=p.net_ev_ci_lower_95,
                        brier=p.brier, log_loss=p.log_loss, fill_prob_maker=p.fill_prob_maker,
                        median_time_to_fill_s=p.median_time_to_fill_s, fdr_pass=p.fdr_pass,
                    )
                )
            n_fdr_bins = sum(1 for b in bins if b.fdr_pass)
            summary = {
                "n_obs": len(obs),
                "n_bins": len(bins),
                "n_fdr_pass_bins": n_fdr_bins,
                "n_perf_rows": len(perf),
                "bins": [
                    [b.label, b.price_bin_lo, b.scope, b.session_integrity_filter,
                     b.n, b.wins, round(b.p_value, 10), b.fdr_pass]
                    for b in sorted(bins, key=lambda x: (x.label, x.scope, x.price_bin_lo,
                                                         x.session_integrity_filter))
                ],
            }
            run = AnalysisRun(
                run_id=run_id, kind="hourly", started_at=started,
                finished_at=dt.datetime.now(dt.UTC),
                window_start=window_start, window_end=window_end,
                n_markets=len({o.label for o in obs}),
                fee_model_version=self.settings.fee_model_version,
                regime_model_version=REGIME_MODEL_VERSION,
                feature_version=FEATURE_VERSION,
                summary_json=summary, summary_hash=_summary_hash(summary), status="ok",
            )
            s.add(run)
            s.commit()
        return run_id
