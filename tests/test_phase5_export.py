"""Phase 5 — Parquet export rowcounts, replay-diff, schema evolution, duckdb."""

from __future__ import annotations

import datetime as dt

import polars as pl
from sqlalchemy import select

from pmre.collectors.orderbook import OrderBook
from pmre.db.models import BtcTick, Market, MarketResolution, Snapshot
from pmre.parquet_export import ParquetExporter, duckdb_query, read_parquet
from pmre.replay import diff_books, rebuild_book_at


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.UTC)


def _seed(db):
    with db.session() as s:
        m = Market(slug="btc-updown-5m-1783447200", price_to_beat=108000.0, fee_rate_bps=72.0)
        s.add(m)
        s.flush()
        s.add(MarketResolution(market_id=m.id, winning_outcome="UP", price_to_beat=108000.0,
                               proxy_end_price=108050.0, margin_bps=4.6, was_close_call=False,
                               resolved_at=utc(2026, 7, 7, 18, 5)))
        for i, label in enumerate(["t_240", "t_120", "t_30"]):
            s.add(Snapshot(
                market_id=m.id, label=label, target_seconds_left=240 - i * 100,
                captured_at=utc(2026, 7, 7, 18, 1, i),
                dominant_side="UP", dominant_mid=0.60 + i * 0.01, dominant_ask=0.62,
                up_mid=0.60, down_mid=0.40, market_spread_proxy=0.02,
                was_correct_mid=True, was_correct_ask=True,
                session_primary="new_york", session_integrity="regular",
            ))
        s.add(BtcTick(source="binance_spot", ts=utc(2026, 7, 7, 18, 1, 0), open=108000.0,
                      high=108010.0, low=107990.0, close=108005.0, volume=1.2, trade_count=5))
        s.commit()
        return m.id


def test_export_rowcount_reconciliation(db, tmp_path):
    _seed(db)
    exp = ParquetExporter(db.session_factory, str(tmp_path))
    manifest = exp.export_day(dt.date(2026, 7, 7))
    assert manifest["row_counts"]["snapshots_features"] == 3
    assert manifest["row_counts"]["btc_1s_bars"] == 1
    assert manifest["row_counts"]["resolutions"] == 1
    # reconcile against DB counts
    with db.session() as s:
        db_snaps = len(s.execute(select(Snapshot)).scalars().all())
    assert manifest["row_counts"]["snapshots_features"] == db_snaps

    # file actually written and readable
    df = pl.read_parquet(str(tmp_path / "dt=2026-07-07" / "snapshots_features.parquet"))
    assert df.height == 3
    assert set(df["label"]) == {"t_240", "t_120", "t_30"}
    assert "feature_version" in df.columns


def test_export_handles_late_appearing_string_in_nullable_column(db, tmp_path):
    """A column null for the first 100+ rows then a string must not break inference.

    Regression: ``session_overlap`` is null outside overlap windows, so an early
    part of the day is all-null and Polars used to infer a Null column and error
    on the first real value ("london_ny_overlap") later in the day.
    """
    day = dt.date(2026, 7, 7)
    with db.session() as s:
        m = Market(slug="btc-updown-5m-1783447200", price_to_beat=108000.0, fee_rate_bps=72.0)
        s.add(m)
        s.flush()
        for i in range(120):
            # first 119 rows have no overlap; the last introduces a string value
            overlap = "london_ny_overlap" if i == 119 else None
            s.add(Snapshot(
                market_id=m.id, label=f"t_{i}", target_seconds_left=i,
                captured_at=utc(2026, 7, 7, 12, 0, 0) + dt.timedelta(seconds=i),
                dominant_side="UP", dominant_mid=0.6, up_mid=0.6, down_mid=0.4,
                session_primary="new_york", session_overlap=overlap, session_integrity="regular",
            ))
        s.commit()

    exp = ParquetExporter(db.session_factory, str(tmp_path))
    manifest = exp.export_day(day)  # must not raise
    assert manifest["row_counts"]["snapshots_features"] == 120
    df = pl.read_parquet(str(tmp_path / "dt=2026-07-07" / "snapshots_features.parquet"))
    assert df["session_overlap"].dtype == pl.String
    assert df.filter(pl.col("session_overlap") == "london_ny_overlap").height == 1


def test_duckdb_accuracy_by_label(db, tmp_path):
    _seed(db)
    ParquetExporter(db.session_factory, str(tmp_path)).export_day(dt.date(2026, 7, 7))
    rows = duckdb_query(
        "SELECT label, count(*) AS n, "
        "avg(CASE WHEN was_correct_mid THEN 1 ELSE 0 END) AS win_rate "
        "FROM read_parquet('{dir}/dt=*/snapshots_features.parquet') "
        "WHERE was_correct_mid IS NOT NULL GROUP BY label ORDER BY label",
        str(tmp_path),
    )
    assert len(rows) == 3
    assert all(r["win_rate"] == 1.0 for r in rows)


def test_schema_evolution_new_column_does_not_break_old_reader(tmp_path):
    # "old" file lacks a column a "new" reader asks for.
    old = pl.DataFrame({"a": [1, 2], "b": [3, 4]})
    path = tmp_path / "old.parquet"
    old.write_parquet(path)
    # new reader requests an added column "c" → tolerated (null-filled)
    df = read_parquet(str(path), columns=["a", "c"])
    assert df["a"].to_list() == [1, 2]
    assert df["c"].to_list() == [None, None]
    # old reader requesting only its known columns from a superset file still works
    new = pl.DataFrame({"a": [1], "b": [2], "c": [9]})
    npath = tmp_path / "new.parquet"
    new.write_parquet(npath)
    df2 = read_parquet(str(npath), columns=["a", "b"])
    assert df2.columns == ["a", "b"]


def test_replay_rebuild_matches_direct_book():
    events = [
        {"event_type": "book", "timestamp": 1783447000000,
         "bids": [{"price": "0.58", "size": "100"}], "asks": [{"price": "0.60", "size": "120"}]},
        {"event_type": "price_change", "timestamp": 1783447001000,
         "changes": [{"price": "0.60", "side": "SELL", "size": "0"}, {"price": "0.59", "side": "SELL", "size": "80"}]},
        {"event_type": "price_change", "timestamp": 1783447050000,
         "changes": [{"price": "0.58", "side": "BUY", "size": "0"}]},
    ]
    # Rebuild as of t=1783447002 (before the 3rd event at ts=...050)
    at = dt.datetime.fromtimestamp(1783447002, tz=dt.UTC)
    replayed = rebuild_book_at(events, at)

    # Direct book with only the first two events applied.
    direct = OrderBook(token_id="t")
    direct.handle(events[0])
    direct.handle(events[1])

    assert diff_books(replayed, direct) == {}
    assert replayed.best_ask() == 0.59
    assert replayed.best_bid() == 0.58


def test_replay_full_stream_equals_final_state():
    events = [
        {"event_type": "book", "timestamp": 1783447000000,
         "bids": [{"price": "0.58", "size": "100"}], "asks": [{"price": "0.60", "size": "120"}]},
        {"event_type": "price_change", "timestamp": 1783447001000,
         "changes": [{"price": "0.59", "side": "SELL", "size": "80"}]},
    ]
    far_future = dt.datetime(2027, 1, 1, tzinfo=dt.UTC)
    book = rebuild_book_at(events, far_future)
    assert set(book.asks) == {0.60, 0.59}
