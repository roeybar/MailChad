"""SQLite source of truth for the vault. Boots the schema; gives connections.

This is the everything-box. Source of truth for contacts, templates,
campaigns, sends, events, suppression. Mirrored to backup on each wake.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("vault.db")

DB_PATH = Path(os.environ.get("VAULT_DB_PATH", "/var/lib/vault/state.sqlite"))
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS _migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    applied = {row[0] for row in conn.execute("SELECT name FROM _migration")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        log.info("applying migration: %s", path.name)
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


def audit(actor: str, action: str, target: str | None = None, details: dict | None = None) -> None:
    """Append to the audit log. Use liberally - this is the after-the-fact trail."""
    import json
    with conn() as c:
        c.execute(
            "INSERT INTO audit_event (actor, action, target, details_json) VALUES (?, ?, ?, ?)",
            (actor, action, target, json.dumps(details or {})),
        )
        c.commit()
