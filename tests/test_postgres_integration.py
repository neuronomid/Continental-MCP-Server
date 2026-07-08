"""Opt-in PostgreSQL + TimescaleDB integration test.

Runs only when ``PMRE_TEST_POSTGRES_URL`` is set (CI's timescaledb service or a
local container). Validates the exact production path SQLite can't: TimescaleDB
hypertable creation (partitioning-column PK widening), compression policies, and
a round-trip through a real backend.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import select, text

from pmre.db.engine import Database
from pmre.db.models import BtcTick, Market

PG_URL = os.environ.get("PMRE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="PMRE_TEST_POSTGRES_URL not set")


@pytest.fixture
def pg():
    db = Database(PG_URL)
    db.drop_all()
    db.create_all()
    db.apply_timescale()
    yield db
    db.drop_all()


def test_hypertables_created_with_compression(pg):
    with pg.engine.connect() as c:
        hts = {r[0] for r in c.execute(
            text("SELECT hypertable_name FROM timescaledb_information.hypertables")
        )}
        assert {"btc_ticks", "trade_prints", "system_health_events"} <= hts
        policies = c.execute(text(
            "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name='policy_compression'"
        )).scalar()
        assert policies >= 3


def test_round_trip_on_postgres(pg):
    with pg.session() as s:
        s.add(Market(slug="btc-updown-5m-pg", price_to_beat=108000.0))
        s.add(BtcTick(source="binance_spot", ts=dt.datetime.now(dt.UTC), close=108000.0))
        s.commit()
    with pg.session() as s:
        assert s.execute(select(Market)).scalar_one().slug == "btc-updown-5m-pg"
        assert s.execute(select(BtcTick)).scalar_one().close == 108000.0
