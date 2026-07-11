"""Shared pytest fixtures.

The suite runs on SQLite (portable, no Docker) with the same ORM models used
against PostgreSQL + TimescaleDB in production. A ``testcontainers`` Postgres run
is available opt-in via ``PMRE_TEST_POSTGRES=1`` for the schema/round-trip tests.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pmre.config import Settings  # noqa: E402
from pmre.db.engine import Database  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Keep tests independent of the operator's local ``./.env``.

    On a deployed host ``.env`` carries real secrets and a Postgres URL; pydantic
    would load it and mask the code defaults / fail-fast behaviour these tests
    assert on. Neutralise the env-file for every test (explicit overrides and
    real env vars still apply).
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def db(tmp_path) -> Database:
    url = f"sqlite+pysqlite:///{tmp_path/'pmre_test.db'}"
    database = Database(url)
    database.create_all()
    return database


@pytest.fixture
def session_factory(db):
    return db.session_factory


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        env="dev",
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=str(tmp_path),
        parquet_dir=str(tmp_path / "parquet"),
        raw_dir=str(tmp_path / "raw"),
        rest_bearer_tokens="read-token-1,read-token-2",
        mcp_bearer_token="mcp-token",
        ingest_bearer_token="ingest-token",
    )


@pytest.fixture
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def fixture_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "fixtures" / name
