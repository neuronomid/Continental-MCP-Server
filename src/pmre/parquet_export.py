"""Daily Parquet exporter + duckdb helper + raw→feature replay.

Produces analysis-ready, reproducible datasets partitioned by date::

    /data/parquet/dt=YYYY-MM-DD/
        snapshots_features.parquet   (snapshot ⨝ resolution ⨝ market ⨝ fees)
        btc_1s_bars.parquet
        trade_prints.parquet
        resolutions.parquet
        manifest.json                (row counts, feature_version, exported_at)

Partitions are never mutated in place — a correction re-exports the whole day and
bumps the manifest version (mcp_phases.md Phase 5 heads-up).
"""

from __future__ import annotations

import datetime as dt
import json
import os

import polars as pl
from sqlalchemy import select

from . import FEATURE_VERSION
from .db.models import BtcTick, Market, MarketResolution, Snapshot, TradePrint


def _naive(d: dt.datetime | None):
    if d is None:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.UTC).replace(tzinfo=None)
    return d


def _day_bounds(date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(date, dt.time(0, 0), tzinfo=dt.UTC)
    return start, start + dt.timedelta(days=1)


def build_snapshots_features(session_factory, date: dt.date) -> pl.DataFrame:
    start, end = _day_bounds(date)
    rows: list[dict] = []
    with session_factory() as s:
        markets = {m.id: m for m in s.execute(select(Market)).scalars()}
        resolutions = {
            r.market_id: r for r in s.execute(select(MarketResolution)).scalars()
        }
        snaps = s.execute(
            select(Snapshot).where(
                Snapshot.captured_at >= start, Snapshot.captured_at < end
            )
        ).scalars()
        for snap in snaps:
            m = markets.get(snap.market_id)
            r = resolutions.get(snap.market_id)
            rows.append(
                {
                    "snapshot_id": snap.id,
                    "market_id": snap.market_id,
                    "slug": m.slug if m else None,
                    "label": snap.label,
                    "target_seconds_left": snap.target_seconds_left,
                    "snapshot_actual_seconds_left": snap.snapshot_actual_seconds_left,
                    "captured_at": _naive(snap.captured_at),
                    "dominant_side": snap.dominant_side,
                    "dominant_mid": snap.dominant_mid,
                    "dominant_ask": snap.dominant_ask,
                    "up_mid": snap.up_mid,
                    "down_mid": snap.down_mid,
                    "market_spread_proxy": snap.market_spread_proxy,
                    "max_usd_buy_within_2c": snap.max_usd_buy_within_2c,
                    "taker_fee_est_dominant": snap.taker_fee_est_dominant,
                    "p_fair": snap.p_fair,
                    "model_edge": snap.model_edge,
                    "z_score": snap.z_score,
                    "sigma_1s": snap.sigma_1s,
                    "session_primary": snap.session_primary,
                    "session_overlap": snap.session_overlap,
                    "session_integrity": snap.session_integrity,
                    "regime": snap.regime,
                    "stale_book_flag": snap.stale_book_flag,
                    "crossed_book_flag": snap.crossed_book_flag,
                    "bad_sum_flag": snap.bad_sum_flag,
                    "price_to_beat": (m.price_to_beat if m else None),
                    "fee_rate_bps": (m.fee_rate_bps if m else None),
                    "winning_outcome": (r.winning_outcome if r else None),
                    "was_close_call": snap.was_close_call,
                    "was_correct_mid": snap.was_correct_mid,
                    "was_correct_ask": snap.was_correct_ask,
                    "feature_version": snap.feature_version or FEATURE_VERSION,
                }
            )
    # infer_schema_length=None scans every row for dtype inference. Without it,
    # a column that is null for the first 100 rows (e.g. session_overlap outside
    # overlap windows, or regime) is inferred as Null and then errors when a real
    # value ("london_ny_overlap") appears later in the day.
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def _simple_table(session_factory, model, ts_col: str, date: dt.date, cols: list[str]) -> pl.DataFrame:
    start, end = _day_bounds(date)
    col = getattr(model, ts_col)
    rows: list[dict] = []
    with session_factory() as s:
        for obj in s.execute(select(model).where(col >= start, col < end)).scalars():
            rows.append({c: _naive(getattr(obj, c)) if isinstance(getattr(obj, c), dt.datetime) else getattr(obj, c) for c in cols})
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


class ParquetExporter:
    def __init__(self, session_factory, out_root: str, feature_version: str = FEATURE_VERSION):
        self.session_factory = session_factory
        self.out_root = out_root
        self.feature_version = feature_version

    def _dir(self, date: dt.date) -> str:
        d = os.path.join(self.out_root, f"dt={date.isoformat()}")
        os.makedirs(d, exist_ok=True)
        return d

    def export_day(self, date: dt.date, manifest_version: int = 1) -> dict:
        out = self._dir(date)
        datasets = {
            "snapshots_features": build_snapshots_features(self.session_factory, date),
            "btc_1s_bars": _simple_table(
                self.session_factory, BtcTick, "ts", date,
                ["id", "ts", "source", "open", "high", "low", "close", "volume", "trade_count"],
            ),
            "trade_prints": _simple_table(
                self.session_factory, TradePrint, "ts", date,
                ["id", "market_id", "token_id", "outcome", "price", "size", "side", "ts", "seconds_left"],
            ),
            # Partition resolutions by the domain resolution time (resolved_at),
            # not by row-insertion time, so a day's export is stable and correct.
            "resolutions": _simple_table(
                self.session_factory, MarketResolution, "resolved_at", date,
                ["market_id", "winning_outcome", "price_to_beat", "proxy_end_price",
                 "margin_bps", "was_close_call", "tie_rule_applied", "resolved_at"],
            ),
        }
        counts = {}
        for name, df in datasets.items():
            path = os.path.join(out, f"{name}.parquet")
            if df.height == 0:
                # write an empty file marker so readers see the partition exists
                pl.DataFrame().write_parquet(path)
            else:
                df.write_parquet(path, compression="zstd")
            counts[name] = df.height

        manifest = {
            "date": date.isoformat(),
            "manifest_version": manifest_version,
            "feature_version": self.feature_version,
            "exported_at": dt.datetime.now(dt.UTC).isoformat(),
            "row_counts": counts,
        }
        with open(os.path.join(out, "manifest.json"), "w") as fh:
            json.dump(manifest, fh, indent=2)
        return manifest


def read_parquet(path: str, columns: list[str] | None = None) -> pl.DataFrame:
    """Tolerant reader: requested columns absent from an older file yield nulls."""
    df = pl.read_parquet(path)
    if columns is None:
        return df
    missing = [c for c in columns if c not in df.columns]
    for c in missing:
        df = df.with_columns(pl.lit(None).alias(c))
    return df.select(columns)


def duckdb_query(sql: str, parquet_dir: str) -> list[dict]:
    """Run a duckdb SQL query; ``{dir}`` in the SQL is replaced by ``parquet_dir``."""
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("SET threads TO 2")
        result = con.execute(sql.replace("{dir}", parquet_dir)).fetchall()
        cols = [d[0] for d in con.description]
        return [dict(zip(cols, row, strict=True)) for row in result]
    finally:
        con.close()
