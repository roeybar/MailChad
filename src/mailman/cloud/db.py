"""SQLite for the cloud - sync log + packs + audit (v3.1)."""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("cloud.db")

DB_PATH = Path(os.environ.get("CLOUD_DB_PATH", "/var/lib/cloud/cloud.sqlite"))
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS _migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    applied = {row[0] for row in conn.execute("SELECT name FROM _migration")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        log.info("applying cloud migration: %s", path.name)
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO _migration (name) VALUES (?)", (path.name,))
        conn.commit()


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA foreign_keys = ON")
        _apply_migrations(c)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA foreign_keys = ON")
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()
