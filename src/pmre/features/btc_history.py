"""DB-backed BTC proxy helpers for the live snapshotter / resolution loops.

The ``btc_feed`` collector writes 1-second OHLC rows per source into ``btc_ticks``.
A *separate* process (snapshotter, resolution) cannot share that collector's
in-memory :class:`BtcFeatureState`, so it reconstructs the rolling state and any
point-in-time proxy price by replaying those rows from the database.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from .btc_state import BtcFeatureState


def _as_utc(instant: dt.datetime) -> dt.datetime:
    return instant if instant.tzinfo else instant.replace(tzinfo=dt.UTC)


def btc_price_at(
    session_factory, instant: dt.datetime, source: str = "binance_spot"
) -> float | None:
    """Latest ``close`` at or before ``instant`` for ``source`` (falls back to any source)."""
    from ..db.models import BtcTick

    instant = _as_utc(instant)
    with session_factory() as s:
        row = s.execute(
            select(BtcTick.close)
            .where(BtcTick.source == source, BtcTick.ts <= instant)
            .order_by(BtcTick.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = s.execute(
                select(BtcTick.close)
                .where(BtcTick.ts <= instant)
                .order_by(BtcTick.ts.desc())
                .limit(1)
            ).scalar_one_or_none()
    return float(row) if row is not None else None


def build_btc_feature_state(
    session_factory,
    as_of: dt.datetime,
    history_s: int = 600,
    primary: str = "binance_spot",
    secondary: str = "coinbase",
) -> tuple[BtcFeatureState, float | None]:
    """Replay ``history_s`` of primary-source ticks into a fresh BtcFeatureState.

    Returns ``(state, secondary_price)`` where ``secondary_price`` is the other
    feed's latest close (feeds the dual-source divergence flag).
    """
    from ..db.models import BtcTick

    as_of = _as_utc(as_of)
    lo = as_of - dt.timedelta(seconds=history_s)
    fs = BtcFeatureState(history_s=history_s)
    with session_factory() as s:
        rows = s.execute(
            select(BtcTick.ts, BtcTick.close, BtcTick.trade_count)
            .where(BtcTick.source == primary, BtcTick.ts >= lo, BtcTick.ts <= as_of)
            .order_by(BtcTick.ts.asc())
        ).all()
    for ts, close, trade_count in rows:
        fs.update(_as_utc(ts).timestamp(), float(close), int(trade_count or 0))
    return fs, btc_price_at(session_factory, as_of, secondary)
