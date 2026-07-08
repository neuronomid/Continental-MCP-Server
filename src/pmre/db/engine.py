"""Engine / session factory and schema creation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import HYPERTABLES, Base


def make_engine(url: str, echo: bool = False) -> Engine:
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        # Enforce FK constraints on SQLite (off by default).
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def apply_timescale_policies(engine: Engine, compress_after_days: int = 7) -> None:
    """Enable TimescaleDB + convert hypertables (Postgres only; no-op elsewhere)."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        for table, time_col in HYPERTABLES.items():
            if time_col == "id":
                continue  # id-partitioned tables handled by app-level partitioning
            # TimescaleDB requires the partitioning column to be part of any
            # unique index / primary key. Our surrogate `id` PK does not include
            # `ts`, so widen the PK to (id, ts) before creating the hypertable.
            # `id` keeps its identity/sequence default, so inserts still work.
            conn.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey'))
            conn.execute(text(f'ALTER TABLE {table} ADD PRIMARY KEY (id, {time_col})'))
            conn.execute(
                text(
                    "SELECT create_hypertable(:t, :c, if_not_exists => TRUE, "
                    "migrate_data => TRUE)"
                ),
                {"t": table, "c": time_col},
            )
            conn.execute(
                text(f"ALTER TABLE {table} SET (timescaledb.compress)")
            )
            conn.execute(
                text(
                    "SELECT add_compression_policy(:t, INTERVAL :i, if_not_exists => TRUE)"
                ),
                {"t": table, "i": f"{compress_after_days} days"},
            )


class Database:
    """Thin owner of an engine + session factory."""

    def __init__(self, url: str, echo: bool = False):
        self.url = url
        self.engine = make_engine(url, echo=echo)
        self.session_factory = make_session_factory(self.engine)

    def create_all(self) -> None:
        create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    def apply_timescale(self) -> None:
        apply_timescale_policies(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
