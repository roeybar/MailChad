"""ORM migration net: the converted db.init() from every
historical level, idempotence, marker durability, baseline schema."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
sys.path.insert(0, str(Path(__file__).parent))
import orm_levels  # noqa: E402
from test_orm_schema import create_all_db, schema_facts  # noqa: E402

BASELINE = Path(__file__).parent / "fixtures" / "migrations_baseline.json"
LEVELS = [("terminal", k) for k in range(len(orm_levels.migration_files("terminal")) + 1)] + \
         [("cloud", k) for k in range(len(orm_levels.migration_files("cloud")) + 1)]


def _sql_list(path) -> list[str]:
    c = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    finally:
        c.close()


def _migration_names(path) -> list[str]:
    c = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in c.execute("SELECT name FROM _migration"))
    finally:
        c.close()


def _init_at(db, path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", Path(path))
    db.init()


def _raw_rows(path, table) -> list[dict]:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        c.close()


@pytest.mark.parametrize("kind,k", LEVELS, ids=[f"{kd}-{k}" for kd, k in LEVELS])
def test_migrate_from_each_level(kind, k, tmp_path, monkeypatch):
    db = importlib.import_module(f"mailman.{kind}.db")
    p, fresh = tmp_path / "level.sqlite", tmp_path / "fresh.sqlite"
    seeded = orm_levels.make_db_at_level(kind, p, k)
    _init_at(db, fresh, monkeypatch)
    _init_at(db, p, monkeypatch)
    assert _migration_names(p) == [f.name for f in orm_levels.migration_files(kind)]
    assert _sql_list(p) == _sql_list(fresh)
    # Every seeded row survives, through RENAMES, with values unchanged.
    for table, rows in seeded.items():
        new_table = orm_levels.RENAMES.get((table, None), (table, None))[0]
        have = _raw_rows(p, new_table)
        for row in rows:
            want = {orm_levels.RENAMES.get((table, col), (None, col))[1]: v for col, v in row.items()}
            assert any(all(type(h.get(col)) is type(v) and h.get(col) == v for col, v in want.items())
                       for h in have), (table, row)   # type-strict
    # The models read every raw row, and equal the schema on this DB.
    from sqlalchemy import create_engine, literal_column, select
    from sqlalchemy.orm import Session
    models = importlib.import_module(f"mailman.{kind}.models")
    eng = create_engine(f"sqlite:///{p}")
    try:
        with Session(eng) as s:
            for mapper in models.Base.registry.mappers:
                raw = _raw_rows(p, mapper.local_table.name)
                objs = s.scalars(select(mapper.class_).order_by(literal_column("rowid"))).all()
                got = [{a.key: getattr(o, a.key) for a in mapper.column_attrs} for o in objs]
                assert got == raw, mapper.local_table.name
    finally:
        eng.dispose()
    created = tmp_path / "created.sqlite"
    create_all_db(models, created)
    assert schema_facts(p) == schema_facts(created)


def test_init_twice_is_noop(tmp_path, monkeypatch):
    for kind in ("terminal", "cloud"):
        db = importlib.import_module(f"mailman.{kind}.db")
        p = tmp_path / f"twice-{kind}.sqlite"
        _init_at(db, p, monkeypatch)
        sql, names = _sql_list(p), _migration_names(p)
        db.init()
        assert _sql_list(p) == sql
        assert _migration_names(p) == names == [f.name for f in orm_levels.migration_files(kind)]


def test_last_migration_marker_survives_reopen(tmp_path, monkeypatch):
    for kind in ("terminal", "cloud"):
        db = importlib.import_module(f"mailman.{kind}.db")
        p = tmp_path / f"reopen-{kind}.sqlite"
        _init_at(db, p, monkeypatch)
        db.engine.cache_clear()
        assert _migration_names(p) == [f.name for f in orm_levels.migration_files(kind)]
        db.init()


def test_fresh_db_equals_baseline(tmp_path, monkeypatch):
    def norm(s: str) -> str:
        return s.replace("\u2026", "...")

    baseline = json.loads(BASELINE.read_text(encoding="ascii"))["terminal_schema"]
    p = tmp_path / "level18.sqlite"
    orm_levels.make_db_at_level("terminal", p, 18, seed=False)
    assert sorted(norm(s) for s in _sql_list(p)) == sorted(norm(s) for s in baseline)
    db = importlib.import_module("mailman.terminal.db")
    q, r = tmp_path / "raw-all.sqlite", tmp_path / "init.sqlite"
    orm_levels.make_db_at_level("terminal", q, len(orm_levels.migration_files("terminal")), seed=False)
    _init_at(db, r, monkeypatch)
    assert _sql_list(r) == _sql_list(q)
