"""Historical-level databases for the ORM migration tests.

make_db_at_level(kind, path, k) applies the first k migration files of the
terminal or cloud layer with a verbatim copy of the historical raw-sqlite3
_apply_migrations, then seeds 2 rows into every table present. Raw sqlite3
only: this module is part of the independent reference.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("tests.orm_levels")

SRC = Path(__file__).resolve().parents[2] / "src" / "mailman"
MIGRATIONS_DIRS = {
    "terminal": SRC / "terminal" / "migrations",
    "cloud": SRC / "cloud" / "migrations",
}

# Rows seeded below terminal level 12 live under these names after 0012.
RENAMES = {
    ("companies", None): ("entities", None),
    ("campaigns", "company_id"): ("campaigns", "entity_id"),
}

_CHECK_IN = re.compile(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)\s*\)", re.I | re.S)


def migration_files(kind: str) -> list[Path]:
    return sorted(MIGRATIONS_DIRS[kind].glob("*.sql"))


def _apply_migrations(conn: sqlite3.Connection, files: list[Path]) -> None:
    # Verbatim historical db._apply_migrations; the file list is the first k files.
    conn.execute("CREATE TABLE IF NOT EXISTS _migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    applied = {row[0] for row in conn.execute("SELECT name FROM _migration")}
    for path in files:
        if path.name in applied:
            continue
        log.info("applying migration: %s", path.name)
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO _migration (name) VALUES (?)", (path.name,))
        conn.commit()


def make_db_at_level(kind: str, path, k: int, *, seed: bool = True) -> dict[str, list[dict]]:
    """Build a `kind` DB at migration level k. Returns the seeded rows per table."""
    c = sqlite3.connect(str(path))
    try:
        c.execute("PRAGMA foreign_keys = ON")
        _apply_migrations(c, migration_files(kind)[:k])
        rows = seed_db(c) if seed else {}
        c.commit()
    finally:
        c.close()
    return rows


def check_literals(sql: str) -> dict[str, list[str]]:
    return {m.group(1): re.findall(r"'([^']*)'", m.group(2)) for m in _CHECK_IN.finditer(sql)}


def seed_tables(c: sqlite3.Connection) -> list[str]:
    """Tables to seed, in foreign_key_list dependency order."""
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT IN ('_migration', 'sqlite_sequence') ORDER BY name")]
    parents = {t: {r[2] for r in c.execute(f"PRAGMA foreign_key_list('{t}')")} - {t} for t in tables}
    order: list[str] = []
    while len(order) < len(tables):
        ready = [t for t in tables if t not in order and parents[t] <= set(order)]
        if not ready:
            raise RuntimeError(f"FK cycle among {sorted(set(tables) - set(order))}")
        order.extend(ready)
    return order


def pk_columns(c: sqlite3.Connection, table: str) -> list[str]:
    cols = sorted((r[5], r[1]) for r in c.execute(f"PRAGMA table_xinfo('{table}')") if r[5] > 0)
    return [name for _, name in cols]


def seed_db(c: sqlite3.Connection) -> dict[str, list[dict]]:
    seeded: dict[str, list[dict]] = {}
    for t in seed_tables(c):
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
        checks = check_literals(sql)
        fks = {r[3]: (r[2], r[4]) for r in c.execute(f"PRAGMA foreign_key_list('{t}')")}
        cols = list(c.execute(f"PRAGMA table_xinfo('{t}')"))
        seeded[t] = []
        before = c.execute(f"SELECT coalesce(max(rowid), 0) FROM {t}").fetchone()[0]
        for i in range(2):
            row: dict = {}
            for _cid, name, typ, notnull, dflt, pk, hidden in cols:
                if hidden:
                    continue
                if name in fks:
                    ptab, pcol = fks[name]
                    pcol = pcol or pk_columns(c, ptab)[0]
                    row[name] = seeded[ptab][i][pcol]
                    continue
                if name in checks:
                    row[name] = checks[name][i]
                    continue
                if pk == 0 and notnull and dflt is not None:
                    continue
                if pk == 0 and not notnull and i == 0:
                    row[name] = None
                    continue
                typ = (typ or "").upper()
                if "INT" in typ:
                    row[name] = i + 1
                elif "REAL" in typ:
                    row[name] = i + 0.5
                elif "BLOB" in typ:
                    row[name] = bytes([i]) * 32
                else:
                    row[name] = f"{t}-{name}-{i}"
            names = list(row)
            c.execute(
                f"INSERT INTO {t} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                [row[n] for n in names],
            )
            seeded[t].append(row)
        # Read back the complete rows (defaults included): survival is checked on every column.
        cur = c.execute(f"SELECT * FROM {t} WHERE rowid > ? ORDER BY rowid", (before,))
        names = [d[0] for d in cur.description]
        seeded[t] = [dict(zip(names, r)) for r in cur.fetchall()]
    return seeded
