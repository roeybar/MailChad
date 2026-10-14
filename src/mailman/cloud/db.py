"""SQLite for the cloud - sync log + packs + audit (v3.1).

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

from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from mailman.cloud.models import Migration

log = logging.getLogger("cloud.db")

DB_PATH = Path(os.environ.get("CLOUD_DB_PATH", "/var/lib/cloud/cloud.sqlite"))
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
        log.info("applying cloud migration: %s", path.name)
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
