"""SQLite source of truth for the vault. Boots the schema; gives connections.

This is the everything-box. Source of truth for contacts, templates,
campaigns, sends, events, suppression. Mirrored to backup on each wake.

SQLAlchemy over the same sqlite3 connections: per-call
connect/close (NullPool), WAL, the busy timeout and legacy transaction
control are unchanged. The migrations stay the schema source.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event, func, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from mailman.terminal.models import AuditEvent, Migration

log = logging.getLogger("vault.db")

DB_PATH = Path(os.environ.get("VAULT_DB_PATH", "/var/lib/vault/state.sqlite"))
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BOOTSTRAP_DDL = "CREATE TABLE IF NOT EXISTS _migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"


def _foreign_keys_on(dbapi_conn, _record) -> None:
    dbapi_conn.execute("PRAGMA foreign_keys = ON")


@lru_cache(maxsize=None)
def _engine(p: str, timeout: float) -> Engine:
    eng = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(p, timeout=timeout), poolclass=NullPool,
                        hide_parameters=True)
    event.listen(eng, "connect", _foreign_keys_on)
    return eng


def engine(timeout: float = 10.0, path=None) -> Engine:
    """Cached per (str(path or DB_PATH), timeout); DB_PATH is read at call time."""
    return _engine(str(path or DB_PATH), timeout)


engine.cache_clear = _engine.cache_clear
engine.cache_info = _engine.cache_info


@contextmanager
def session(path=None):
    s = Session(engine(10.0, path), expire_on_commit=False, autoflush=False)
    try:
        yield s
    finally:
        s.close()


def begin_explicit(s: Session) -> None:
    s.connection().exec_driver_sql("BEGIN")


def _apply_migrations(sa_conn: Connection) -> None:
    sa_conn.exec_driver_sql(BOOTSTRAP_DDL)
    sa_conn.commit()
    applied = set(sa_conn.execute(select(Migration.name)).scalars())
    drv = sa_conn.connection.driver_connection
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        log.info("applying migration: %s", path.name)
        drv.executescript(path.read_text())          # sqlite3 commits any open transaction first
        sa_conn.execute(insert(Migration).values(name=path.name))
        sa_conn.commit()                             # the marker is durable before the next file


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with engine(5.0).connect() as sa_conn:
        sa_conn.exec_driver_sql("PRAGMA journal_mode = WAL")
        sa_conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        sa_conn.commit()
        _apply_migrations(sa_conn)


def readonly_engine(path) -> Engine:
    """A read-only engine on path; it never creates the file."""
    return create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True),
                         poolclass=NullPool, hide_parameters=True)


@contextmanager
def driver_connection(path, timeout: float = 5.0):
    """A real sqlite3.Connection on path from an uncached engine (online-backup page copies only)."""
    eng = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(str(path), timeout=timeout),
                        poolclass=NullPool, hide_parameters=True)
    proxy = eng.raw_connection()
    try:
        yield proxy.driver_connection
    finally:
        proxy.close()
        eng.dispose()


def integrity_ok(path) -> bool:
    """True when path is a non-empty SQLite file that passes integrity_check and has a migration row."""
    try:
        if Path(path).stat().st_size <= 0:
            return False
        eng = readonly_engine(path)
        try:
            with eng.connect() as c:
                if c.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
                    return False
                return c.execute(select(func.count()).select_from(Migration)).scalar_one() >= 1
        finally:
            eng.dispose()
    except Exception:
        return False


def audit(actor: str, action: str, target: str | None = None, details: dict | None = None) -> None:
    """Append to the audit log. Use liberally - this is the after-the-fact trail."""
    import json
    with session() as s:
        s.execute(
            insert(AuditEvent).values(actor=actor, action=action, target=target,
                                      details_json=json.dumps(details or {}))
            .execution_options(synchronize_session=False)
        )
        s.commit()
