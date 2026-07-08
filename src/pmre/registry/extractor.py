"""Glue: turn FDR-passing calibration bins into walk-forward-validated candidates.

Runs after an analytics run: for each FDR-passing, positive-edge bin with enough
sample and a positive CI-lower net EV (after fees), it validates persistence via
walk-forward and, if that passes, extracts a ``research_only`` candidate with full
evidence. Nothing is ever promoted automatically.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

from sqlalchemy import select

from ..analytics.calibration import price_bin
from ..analytics.stats import wilson_lower
from ..analytics.walkforward import DatedObs, WalkForward
from ..db.models import CalibrationBin, Market, MarketResolution, Snapshot
from ..feecurve import taker_fee_per_share
from .candidates import CandidateEvidence, CandidateRegistry


@dataclass
class DatedSnapshotObs:
    market_id: int
    date: dt.date
    label: str
    scope_session: str | None
    session_integrity: str
    dominant_mid: float
    dominant_ask: float | None
    won: int
    max_usd: float | None
    fee_rate: float


def load_dated_obs(session_factory) -> list[DatedSnapshotObs]:
    out: list[DatedSnapshotObs] = []
    with session_factory() as s:
        resolutions = {r.market_id for r in s.execute(select(MarketResolution)).scalars()}
        markets = {m.id: m for m in s.execute(select(Market)).scalars()}
        for snap in s.execute(select(Snapshot)).scalars():
            if snap.market_id not in resolutions or snap.was_correct_mid is None:
                continue
            if snap.dominant_mid is None:
                continue
            m = markets.get(snap.market_id)
            fee_rate = (m.fee_rate_bps / 1000.0) if (m and m.fee_rate_bps) else 0.072
            out.append(
                DatedSnapshotObs(
                    market_id=snap.market_id,
                    date=snap.captured_at.date(),
                    label=snap.label,
                    scope_session=snap.session_primary,
                    session_integrity=snap.session_integrity or "regular",
                    dominant_mid=snap.dominant_mid,
                    dominant_ask=snap.dominant_ask,
                    won=1 if snap.was_correct_mid else 0,
                    max_usd=snap.max_usd_buy_within_2c,
                    fee_rate=fee_rate,
                )
            )
    return out


def _matches_scope(o: DatedSnapshotObs, scope: str) -> bool:
    if scope == "total":
        return True
    if scope.startswith("session:"):
        return o.scope_session == scope.split(":", 1)[1]
    return False


class CandidateExtractor:
    def __init__(self, session_factory, settings=None, min_n: int = 200):
        self.session_factory = session_factory
        self.settings = settings
        self.min_n = min_n
        self.registry = CandidateRegistry(session_factory)
        self.walk_forward = WalkForward()

    def extract_from_run(self, run_id: str) -> list[str]:
        with self.session_factory() as s:
            bins = s.execute(
                select(CalibrationBin).where(
                    CalibrationBin.run_id == run_id,
                    CalibrationBin.fdr_pass.is_(True),
                    CalibrationBin.edge > 0,
                    CalibrationBin.session_integrity_filter == "regular",
                )
            ).scalars().all()
            bin_specs = [
                (b.label, b.price_bin_lo, b.price_bin_hi, b.scope, b.n, b.wins, b.win_rate,
                 b.avg_entry_price, b.wilson_lo, b.edge)
                for b in bins
            ]

        all_obs = load_dated_obs(self.session_factory)
        evidences: list[CandidateEvidence] = []
        for (label, lo, hi, scope, n, wins, win_rate, avg_mid, _wilson_lo, edge) in bin_specs:
            if n < self.min_n:
                continue
            # entry for a taker is the ask; approximate with the bin's avg ask.
            bin_obs = [
                o for o in all_obs
                if o.label == label and _matches_scope(o, scope)
                and o.session_integrity == "regular"
                and price_bin(o.dominant_mid) == (lo, hi)
            ]
            if not bin_obs:
                continue
            asks = [o.dominant_ask for o in bin_obs if o.dominant_ask is not None]
            entry_ask = statistics.mean(asks) if asks else avg_mid
            fee_rate = bin_obs[0].fee_rate
            fee = taker_fee_per_share(entry_ask, fee_rate)
            net_ev = win_rate - entry_ask - fee
            ci_lower = wilson_lower(wins, n) - entry_ask - fee
            if ci_lower <= 0:
                continue

            wf_obs = [
                DatedObs(market_id=o.market_id, date=o.date, won=o.won, entry_price=entry_ask,
                         fee_rate=fee_rate)
                for o in bin_obs
            ]
            wf = self.walk_forward.evaluate(wf_obs)

            liquidity = [o.max_usd for o in bin_obs if o.max_usd is not None]
            median_liq = statistics.median(liquidity) if liquidity else None

            evidences.append(
                CandidateEvidence(
                    label=label, price_bin_lo=lo, price_bin_hi=hi, scope=scope,
                    entry_style="taker_ask", direction="dominant", n=n, win_rate=win_rate,
                    net_ev=net_ev, net_ev_ci_lower_95=ci_lower, edge=edge,
                    median_liquidity_usd=median_liq, fdr_pass=True,
                    walk_forward_pass=wf.passed,
                    filters={"scope": scope, "integrity": "regular"},
                )
            )
        return self.registry.extract(evidences)
