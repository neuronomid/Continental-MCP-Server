"""Backup/restore helpers (nightly pg_dump for prod; sqlite copy for dev/tests).

The restore path is intentionally exercised in tests — a backup you have never
restored is not a backup (Phase-10 acceptance: successful restore on a scratch DB).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path


def pg_dump_command(database_url: str, out_path: str) -> list[str]:
    """Build a ``pg_dump`` argv for the given SQLAlchemy Postgres URL."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    argv = ["pg_dump", "--no-owner", "--format=custom", "-f", out_path]
    if url.host:
        argv += ["-h", url.host]
    if url.port:
        argv += ["-p", str(url.port)]
    if url.username:
        argv += ["-U", url.username]
    argv += [url.database]
    return argv


def sqlite_backup(src_db_path: str, dest_path: str) -> str:
    """Consistent online backup of a SQLite DB using the backup API."""
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(src_db_path)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest_path


def sqlite_restore(backup_path: str, dest_db_path: str) -> str:
    Path(dest_db_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup_path, dest_db_path)
    return dest_db_path
