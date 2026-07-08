"""Synthetic end-to-end dataset + full-pipeline driver.

Lets the whole engine be exercised offline: build a realistic dataset with a
*planted* NY-session calibration edge, run analytics → extraction, and confirm a
candidate emerges (and that a null dataset yields none). Used by the CLI
``pipeline-demo`` command and the integration test.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from .analytics.runner import HourlyAnalytics
from .config import Settings
from .db.models import Market, MarketResolution, Snapshot
from .feecurve import taker_fee_per_share
from .registry.extractor import CandidateExtractor

LABELS = ("t_270", "t_240", "t_210", "t_180", "t_150", "t_120", "t_90", "t_60", "t_30")


def build_demo_dataset(
    session_factory,
    days: int = 25,
    markets_per_day: int = 120,
    seed: int = 20260707,
    planted_edge: float = 0.15,
    planted_bin_lo: float = 0.60,
    planted_fraction: float = 0.5,
) -> dict:
    """Create markets/snapshots/resolutions with a planted NY-session edge.

    In price bin ``[planted_bin_lo, +0.02)`` during the NY regular session, the
    dominant side wins at ``price + planted_edge`` — a real, persistent,
    discoverable miscalibration. A ``planted_fraction`` of markets are drawn
    directly into that bin so the family clears the sample-size floor at every
    label and in every walk-forward validation window; the rest are spread across
    prices and calibrated. Every other cell is calibrated.
    """
    rng = np.random.default_rng(seed)
    base = dt.date(2026, 6, 1)
    n_markets = 0
    with session_factory() as s:
        for day in range(days):
            d = base + dt.timedelta(days=day)
            for k in range(markets_per_day):
                n_markets += 1
                # Anchor most snapshots in the NY session (14:00–19:00 UTC) so the
                # planted edge has a real NY population.
                captured = dt.datetime.combine(d, dt.time(15, 0), tzinfo=dt.UTC) + dt.timedelta(
                    minutes=int(rng.integers(0, 120))
                )
                m = Market(
                    slug=f"btc-updown-5m-demo-{day}-{k}", fee_rate_bps=72.0,
                    price_to_beat=108000.0, fees_enabled=True, tick_size=0.001,
                    expected_resolution_time_utc=captured + dt.timedelta(seconds=240),
                )
                s.add(m)
                s.flush()

                # Draw a single "favorite mid" for this market and its true win prob.
                if rng.random() < planted_fraction:
                    mid = float(planted_bin_lo + rng.uniform(0.0, 0.02))
                else:
                    mid = float(np.clip(rng.uniform(0.52, 0.90), 0.50, 0.97))
                in_planted = planted_bin_lo <= mid < planted_bin_lo + 0.02
                true_p = mid + (planted_edge if in_planted else 0.0)
                true_p = float(np.clip(true_p, 0.01, 0.99))
                won = int(rng.random() < true_p)
                s.add(MarketResolution(
                    market_id=m.id, winning_outcome="UP" if won else "DOWN",
                    price_to_beat=108000.0, proxy_end_price=108000.0 * (1 + (0.001 if won else -0.001)),
                    was_close_call=False,
                ))
                for label in LABELS:
                    offset = int(label[2:])
                    jitter = float(rng.normal(0, 0.0015))
                    lmid = float(np.clip(mid + jitter, 0.50, 0.979))
                    s.add(Snapshot(
                        market_id=m.id, label=label, target_seconds_left=offset,
                        captured_at=captured + dt.timedelta(seconds=240 - offset),
                        dominant_side="UP", dominant_mid=lmid, dominant_ask=lmid + 0.01,
                        up_best_bid=lmid - 0.01, down_best_bid=0.30,
                        max_usd_buy_within_2c=float(rng.uniform(8, 30)),
                        session_primary="new_york", session_integrity="regular",
                        was_correct_mid=bool(won),
                        taker_fee_est_dominant=taker_fee_per_share(lmid + 0.01),
                    ))
        s.commit()
    return {"markets": n_markets, "days": days}


def run_full_pipeline(session_factory, settings: Settings | None = None) -> dict:
    """Run analytics → candidate extraction; return a summary dict."""
    settings = settings or Settings()
    run_id = HourlyAnalytics(session_factory, settings).run(fdr_min_n=200)
    extractor = CandidateExtractor(session_factory, settings, min_n=200)
    created = extractor.extract_from_run(run_id)
    return {"run_id": run_id, "candidates_created": created, "n_candidates": len(created)}
