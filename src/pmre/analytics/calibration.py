"""Calibration bins + timestamp performance (dual-scope, FDR-controlled).

Primary analytic (mcp_plan.md §1.4): reliability curves — empirical win rate vs
price per bucket — with Wilson CIs and BH-FDR. Every metric is emitted at
``scope=total`` AND per session/overlap on the ``regular``-integrity population;
holiday/half_day/weekend instances are aggregated into their own buckets.
"""

from __future__ import annotations

from dataclasses import dataclass

from pm_sessions import sessions_open_for

from .ev import compute_ev
from .stats import (
    benjamini_hochberg,
    brier_score,
    calibration_pvalue,
    log_loss,
    wilson_interval,
)

# 2¢ price bins from 0.50 to 0.98.
BIN_LO = 0.50
BIN_HI = 0.98
BIN_WIDTH = 0.02


@dataclass
class SnapshotObs:
    label: str
    dominant_side: str | None
    dominant_mid: float | None
    dominant_ask: float | None
    dominant_bid: float | None
    won: int  # 1 if dominant side won
    session_primary: str | None = None
    session_overlap: str | None = None
    session_integrity: str = "regular"
    regime: str | None = None
    fee_rate: float = 0.072
    max_usd: float | None = None
    was_close_call: bool = False
    quality_ok: bool = True


def price_bin(price: float) -> tuple[float, float] | None:
    if price is None or price < BIN_LO or price >= BIN_HI:
        return None
    # + epsilon before floor so exact bin edges (e.g. 0.60) don't fall into the
    # lower bin due to binary float representation of decimal prices.
    import math

    idx = int(math.floor((price - BIN_LO) / BIN_WIDTH + 1e-9))
    lo = round(BIN_LO + idx * BIN_WIDTH, 2)
    return lo, round(lo + BIN_WIDTH, 2)


def _usable(o: SnapshotObs) -> bool:
    return (
        o.quality_ok
        and not o.was_close_call
        and o.dominant_mid is not None
        and BIN_LO <= o.dominant_mid < BIN_HI
        and o.won in (0, 1)
    )


def _scopes_for(o: SnapshotObs) -> list[str]:
    scopes = ["total"]
    if o.session_primary == "off_session":
        scopes.append("session:off_session")
    else:
        # Credit every session that was open — not only the priority-winning
        # primary — so the London/NY overlap counts toward London (and the
        # Tokyo/London overlap toward Tokyo), instead of being attributed solely
        # to the higher-priority session. See pm_sessions.sessions_open_for.
        for name in sorted(sessions_open_for(o.session_primary, o.session_overlap)):
            scopes.append(f"session:{name}")
    if o.session_overlap:
        scopes.append(f"overlap:{o.session_overlap}")
    return scopes


@dataclass
class BinResult:
    label: str
    price_bin_lo: float
    price_bin_hi: float
    scope: str
    session_integrity_filter: str
    n: int
    wins: int
    win_rate: float
    avg_entry_price: float
    wilson_lo: float
    wilson_hi: float
    edge: float
    p_value: float
    fdr_pass: bool = False
    regime: str | None = None


def build_calibration_bins(
    obs: list[SnapshotObs], q: float = 0.10, fdr_min_n: int = 50
) -> list[BinResult]:
    """Build reliability bins across all (scope, integrity, label, price-bin) cells.

    BH-FDR is applied jointly across every bin with ``n ≥ fdr_min_n`` in the run.
    """
    buckets: dict[tuple, list[SnapshotObs]] = {}
    for o in obs:
        if not _usable(o):
            continue
        pb = price_bin(o.dominant_mid)
        if pb is None:
            continue
        for scope in _scopes_for(o):
            key = (o.label, pb[0], pb[1], scope, o.session_integrity)
            buckets.setdefault(key, []).append(o)

    results: list[BinResult] = []
    for (label, lo, hi, scope, integrity), rows in buckets.items():
        n = len(rows)
        wins = sum(r.won for r in rows)
        win_rate = wins / n
        avg_price = sum(r.dominant_mid for r in rows) / n
        wlo, whi = wilson_interval(wins, n)
        pval = calibration_pvalue(wins, n, avg_price)
        results.append(
            BinResult(
                label=label,
                price_bin_lo=lo,
                price_bin_hi=hi,
                scope=scope,
                session_integrity_filter=integrity,
                n=n,
                wins=wins,
                win_rate=win_rate,
                avg_entry_price=avg_price,
                wilson_lo=wlo,
                wilson_hi=whi,
                edge=win_rate - avg_price,
                p_value=pval,
            )
        )

    # BH-FDR across bins with enough n.
    eligible = [r for r in results if r.n >= fdr_min_n]
    passes = benjamini_hochberg([r.p_value for r in eligible], q=q)
    for r, ok in zip(eligible, passes, strict=True):
        r.fdr_pass = ok
    return results


@dataclass
class TimestampPerf:
    label: str
    scope: str
    session_integrity_filter: str
    entry_style: str
    direction: str
    n: int
    win_rate: float
    avg_price: float
    edge_vs_price: float
    net_ev_taker: float | None
    net_ev_maker: float | None
    net_ev_ci_lower_95: float | None
    brier: float | None
    log_loss: float | None
    fdr_pass: bool = False
    regime: str | None = None
    fill_prob_maker: float | None = None
    median_time_to_fill_s: float | None = None


_ENTRY_STYLES = ("taker_ask", "maker_mid", "maker_join_bid")


def _entry_price(o: SnapshotObs, style: str) -> float | None:
    if style == "taker_ask":
        return o.dominant_ask if o.dominant_ask is not None else o.dominant_mid
    if style == "maker_mid":
        return o.dominant_mid
    if style == "maker_join_bid":
        return o.dominant_bid if o.dominant_bid is not None else o.dominant_mid
    return o.dominant_mid


def build_timestamp_performance(
    obs: list[SnapshotObs],
    q: float = 0.10,
    fdr_min_n: int = 50,
    maker_fill_prob: float = 1.0,
) -> list[TimestampPerf]:
    """Per (label, scope, integrity, entry_style, direction) performance."""
    groups: dict[tuple, list[SnapshotObs]] = {}
    for o in obs:
        if not _usable(o):
            continue
        for scope in _scopes_for(o):
            groups.setdefault((o.label, scope, o.session_integrity), []).append(o)

    results: list[TimestampPerf] = []
    for (label, scope, integrity), rows in groups.items():
        n = len(rows)
        wins = sum(r.won for r in rows)
        fee_rate = rows[0].fee_rate
        forecasts = [r.dominant_mid for r in rows]
        outcomes = [r.won for r in rows]
        brier = brier_score(forecasts, outcomes)
        ll = log_loss(forecasts, outcomes)

        for style in _ENTRY_STYLES:
            entry_prices = [_entry_price(r, style) for r in rows]
            avg_entry = sum(entry_prices) / n
            for direction in ("dominant", "contrarian"):
                if direction == "dominant":
                    d_wins, d_n, d_entry = wins, n, avg_entry
                else:
                    # buying the underdog: win when dominant loses; entry = 1 - avg_entry
                    d_wins, d_n, d_entry = n - wins, n, 1.0 - avg_entry
                d_win_rate = d_wins / d_n
                ev = compute_ev(
                    d_wins, d_n,
                    entry_price_taker=d_entry,
                    entry_price_maker=d_entry if style != "taker_ask" else None,
                    p_fill=maker_fill_prob,
                    fee_rate=fee_rate,
                )
                results.append(
                    TimestampPerf(
                        label=label,
                        scope=scope,
                        session_integrity_filter=integrity,
                        entry_style=style,
                        direction=direction,
                        n=d_n,
                        win_rate=d_win_rate,
                        avg_price=d_entry,
                        edge_vs_price=d_win_rate - d_entry,
                        net_ev_taker=ev.net_ev_taker if style == "taker_ask" else None,
                        net_ev_maker=ev.net_ev_maker if style != "taker_ask" else None,
                        net_ev_ci_lower_95=ev.net_ev_ci_lower_95,
                        brier=brier,
                        log_loss=ll,
                        fill_prob_maker=maker_fill_prob if style != "taker_ask" else None,
                    )
                )

    # FDR across taker_ask/dominant groups (the primary decision family).
    primary = [
        r for r in results
        if r.entry_style == "taker_ask" and r.direction == "dominant" and r.n >= fdr_min_n
    ]
    pvals = [
        calibration_pvalue(round(r.win_rate * r.n), r.n, r.avg_price) for r in primary
    ]
    passes = benjamini_hochberg(pvals, q=q)
    for r, ok in zip(primary, passes, strict=True):
        r.fdr_pass = ok
    return results
