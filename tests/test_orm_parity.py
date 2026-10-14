"""ORM connection parity: PRAGMAs, timeouts, threads, explicit
transactions, path handling, read-only and driver connections."""
from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import threading

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Connection-contract tests run for both layers (the cloud db.py mirrors the terminal one).
LAYERS = pytest.mark.parametrize("layer", ["terminal", "cloud"])


def _tdb(tmp_path, monkeypatch, name="state.sqlite", layer="terminal"):
    db = importlib.import_module(f"mailman.{layer}.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / name)
    db.init()
    return db


@LAYERS
def test_session_parity(layer, tmp_path, monkeypatch):
    db = _tdb(tmp_path, monkeypatch, layer=layer)
    with db.session() as s:
        c = s.connection()
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 10000
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


@LAYERS
def test_init_busy_timeout_5000(layer, tmp_path, monkeypatch):
    db = importlib.import_module(f"mailman.{layer}.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "init.sqlite")
    seen = []
    real = sqlite3.connect

    def spy(*a, **k):
        seen.append(k.get("timeout"))
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    db.engine.cache_clear()
    db.init()
    assert seen and set(seen) == {5.0}
    with db.engine(5.0).connect() as c:
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000


@LAYERS
def test_sessions_across_threads(layer, tmp_path, monkeypatch):
    from sqlalchemy import select
    db = _tdb(tmp_path, monkeypatch, layer=layer)
    errors = []

    def work():
        try:
            for _ in range(5):
                with db.session() as s:
                    s.execute(select(1)).scalar_one()
        except Exception as e:  # ProgrammingError would land here
            errors.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


@LAYERS
def test_session_connects_per_call(layer, tmp_path, monkeypatch):
    # Per-call connect/close (NullPool): each session opens its own DBAPI connection
    # and closes it when the session block ends.
    from sqlalchemy import event, select
    db = _tdb(tmp_path, monkeypatch, layer=layer)
    eng = db.engine()
    connects = []
    closes = []

    def on_connect(_dbapi_conn, _record):
        connects.append(1)

    def on_close(_dbapi_conn, _record):
        closes.append(1)

    event.listen(eng, "connect", on_connect)
    event.listen(eng, "close", on_close)
    try:
        for _ in range(3):
            with db.session() as s:
                s.execute(select(1)).scalar_one()
    finally:
        event.remove(eng, "connect", on_connect)
        event.remove(eng, "close", on_close)
    assert len(connects) == 3
    assert len(closes) == 3


@LAYERS
def test_begin_explicit_opens_transaction(layer, tmp_path, monkeypatch):
    db = _tdb(tmp_path, monkeypatch, layer=layer)
    with db.session() as s:
        db.begin_explicit(s)
        assert s.connection().connection.driver_connection.in_transaction is True


@LAYERS
def test_engine_follows_db_path(layer, tmp_path, monkeypatch):
    from sqlalchemy import select
    db = _tdb(tmp_path, monkeypatch, "a.sqlite", layer=layer)
    with db.session() as s:   # an engine for the first path exists before the switch
        s.execute(select(1)).scalar_one()
    _tdb(tmp_path, monkeypatch, "b.sqlite", layer=layer)
    with db.session() as s:
        files = [r[2] for r in s.connection().exec_driver_sql("PRAGMA database_list")]
    assert files == [str(tmp_path / "b.sqlite")]


@LAYERS
def test_db_error_text_hides_bound_parameters(layer, tmp_path, monkeypatch):
    # hide_parameters=True: a constraint error carries no bound value into its text.
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError
    db = _tdb(tmp_path, monkeypatch, layer=layer)
    models = importlib.import_module(f"mailman.{layer}.models")
    bound = "v1:k2-bound-value-5f0c9a7e1d"
    with db.session() as s:
        s.execute(insert(models.Setting).values(key=bound, value=bound))
        s.commit()
    with pytest.raises(IntegrityError) as ei:
        with db.session() as s:
            s.execute(insert(models.Setting).values(key=bound, value=bound))
            s.commit()
    assert "UNIQUE constraint failed: settings.key" in str(ei.value)
    assert bound not in str(ei.value)


def test_lambda_modules_import_without_sqlalchemy():
    # mangum is a Lambda-only dependency absent from the test image: stub it.
    code = ("import sys, types\n"
            "sys.modules['sqlalchemy'] = None\n"
            "_m = types.ModuleType('mangum'); _m.Mangum = lambda app, **k: app; sys.modules['mangum'] = _m\n"
            "import mailman.cloud.lambda_handler, mailman.cloud.dispatcher_lambda\n"
            # the modules dispatcher_lambda and cloud main import inside functions
            "import boto3, mailman.cloud.dynamo, mailman.cloud.keys_dynamo, mailman.cloud.encryption_cloud\n"
            "assert not [m for m in sys.modules if m.startswith('sqlalchemy.')]\n")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


def test_readonly_engine_never_creates_file(tmp_path, monkeypatch):
    db = _tdb(tmp_path, monkeypatch)
    absent = tmp_path / "absent.sqlite"
    eng = db.readonly_engine(absent)
    try:
        with eng.connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        pass
    assert not absent.exists()


def test_driver_connection_is_sqlite3_and_uncached(tmp_path, monkeypatch):
    db = _tdb(tmp_path, monkeypatch)
    before = db.engine.cache_info().currsize
    with db.driver_connection(tmp_path / "other.sqlite") as c:
        assert isinstance(c, sqlite3.Connection)
        c.execute("SELECT 1")
    assert db.engine.cache_info().currsize == before


def test_backup_helper_uses_its_own_path(tmp_path, monkeypatch):
    from mailman.terminal import db
    own = tmp_path / "own.sqlite"
    monkeypatch.setattr(db, "DB_PATH", own)
    db.init()
    elsewhere = tmp_path / "elsewhere" / "state.sqlite"
    monkeypatch.setattr(db, "DB_PATH", elsewhere)
    spec = importlib.util.spec_from_file_location("p1_backup_under_test", ROOT / "scripts" / "_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    res = mod.check_inbox(own)
    assert res.get("total_inbox") == 0, res
    assert not elsewhere.exists()
