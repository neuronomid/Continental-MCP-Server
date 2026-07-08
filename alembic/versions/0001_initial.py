"""initial schema — v1 + v2/v2.1 additions

Greenfield: the ORM models in ``pmre.db.models`` are the single source of truth,
so migration 0001 materialises the whole metadata and (on Postgres) installs the
TimescaleDB hypertable/compression policies. Idempotent: creating tables that
already exist is skipped via ``checkfirst``.

Revision ID: 0001
Revises:
Create Date: 2026-07-07
"""
from __future__ import annotations

from alembic import op

from pmre.db.engine import apply_timescale_policies
from pmre.db.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    apply_timescale_policies(bind.engine)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
