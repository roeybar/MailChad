"""Unit tests for v3.21 consent catch-back, scope-aware suppression, per-day lock.

Runs against a per-test SQLite with the relevant schema. No cloud needed.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import mailchad.terminal.db as _db
import mailchad.terminal.sync_client as sc
import mailchad.terminal.campaign_lock as cl


def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE suppression_hashes (
            email_hash TEXT PRIMARY KEY,
            reason     TEXT NOT NULL DEFAULT 'unsubscribe',
            added_at   TEXT NOT NULL DEFAULT (datetime('now')),
            source     TEXT,
            scope      TEXT NOT NULL DEFAULT 'all'
        );
        CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE contacts (id INTEGER PRIMARY KEY, email TEXT);
        CREATE TABLE campaigns (
            id INTEGER PRIMARY KEY, name TEXT, kind TEXT DEFAULT 'promotional',
            status TEXT, dispatched_at TEXT, lock_until TEXT,
            unsubs_confirmed_at TEXT, lock_duration_s INTEGER DEFAULT 86400
        );
        CREATE TABLE campaign_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER, contact_id INTEGER, status TEXT,
            clicked_at TEXT
        );
    """)
    conn.commit()
    return conn


def _patch_db(db_path):
    return patch.object(_db, "DB_PATH", Path(db_path))


# _merge_unsubs (Phase 1)

def test_merge_unsubs_inserts_with_scope(tmp_path):
    p = str(tmp_path / "s.sqlite"); _make_db(p)
    with _patch_db(p):
        n = sc._merge_unsubs([
            {"email_hash": "aaa", "scope": "all", "added_at": "2026-01-01T00:00:00Z"},
            {"email_hash": "bbb", "scope": "promotional", "added_at": "2026-01-01T00:01:00Z"},
        ])
    assert n == 2
    conn = sqlite3.connect(p); conn.row_factory = sqlite3.Row
    rows = {r["email_hash"]: r for r in conn.execute("SELECT * FROM suppression_hashes")}
    assert rows["aaa"]["scope"] == "all"
    assert rows["bbb"]["scope"] == "promotional"
    assert rows["aaa"]["reason"] == "unsubscribe"
    assert rows["aaa"]["source"] == "sync:unsub_pull"


def test_merge_unsubs_upgrades_promotional_to_all(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    c.execute("INSERT INTO suppression_hashes (email_hash, reason, scope) VALUES ('x','unsubscribe','promotional')")
    c.commit()
    with _patch_db(p):
        n = sc._merge_unsubs([{"email_hash": "x", "scope": "all", "added_at": "2026-02-01T00:00:00Z"}])
    assert n == 1
    conn = sqlite3.connect(p)
    assert conn.execute("SELECT scope FROM suppression_hashes WHERE email_hash='x'").fetchone()[0] == "all"


def test_merge_unsubs_no_downgrade(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    c.execute("INSERT INTO suppression_hashes (email_hash, reason, scope) VALUES ('x','unsubscribe','all')")
    c.commit()
    with _patch_db(p):
        n = sc._merge_unsubs([{"email_hash": "x", "scope": "promotional", "added_at": "2026-02-01T00:00:00Z"}])
    assert n == 0  # already 'all', promotional is a no-op
    conn = sqlite3.connect(p)
    assert conn.execute("SELECT scope FROM suppression_hashes WHERE email_hash='x'").fetchone()[0] == "all"


def test_merge_unsubs_idempotent(tmp_path):
    p = str(tmp_path / "s.sqlite"); _make_db(p)
    rows = [{"email_hash": "dup", "scope": "all", "added_at": "2026-01-01T00:00:00Z"}]
    with _patch_db(p):
        assert sc._merge_unsubs(rows) == 1
        assert sc._merge_unsubs(rows) == 0
    conn = sqlite3.connect(p)
    assert conn.execute("SELECT count(*) FROM suppression_hashes").fetchone()[0] == 1


# per-day lock (Phase 3)

def test_lock_active_campaign_locks_contacts(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    c.execute("INSERT INTO campaigns (id,name,status,dispatched_at,lock_until) "
              "VALUES (1,'A','dispatched',datetime('now'),datetime('now','+1 day'))")
    c.execute("INSERT INTO campaign_recipients (campaign_id,contact_id,status) VALUES (1,100,'sent')")
    c.commit()
    with _patch_db(p):
        locked = cl.locked_contacts()
    assert 100 in locked
    assert locked[100]["campaign_name"] == "A"


def test_lock_clicked_contact_released(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    c.execute("INSERT INTO campaigns (id,name,status,dispatched_at,lock_until) "
              "VALUES (1,'A','dispatched',datetime('now'),datetime('now','+1 day'))")
    c.execute("INSERT INTO campaign_recipients (campaign_id,contact_id,status,clicked_at) "
              "VALUES (1,100,'clicked',datetime('now'))")
    c.commit()
    with _patch_db(p):
        locked = cl.locked_contacts()
    assert 100 not in locked  # click releases


def test_lock_expired_window_not_locked(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    c.execute("INSERT INTO campaigns (id,name,status,dispatched_at,lock_until) "
              "VALUES (1,'A','dispatched',datetime('now','-2 days'),datetime('now','-1 day'))")
    c.execute("INSERT INTO campaign_recipients (campaign_id,contact_id,status) VALUES (1,100,'sent')")
    c.commit()
    with _patch_db(p):
        locked = cl.locked_contacts()
    assert 100 not in locked  # window elapsed


def test_lock_confirmed_campaign_not_locked(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    c.execute("INSERT INTO campaigns (id,name,status,dispatched_at,lock_until,unsubs_confirmed_at) "
              "VALUES (1,'A','dispatched',datetime('now'),datetime('now','+1 day'),datetime('now'))")
    c.execute("INSERT INTO campaign_recipients (campaign_id,contact_id,status) VALUES (1,100,'sent')")
    c.commit()
    with _patch_db(p):
        locked = cl.locked_contacts()
    assert 100 not in locked


def test_find_conflicts_excludes_self(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    c.execute("INSERT INTO campaigns (id,name,status,dispatched_at,lock_until) "
              "VALUES (1,'A','dispatched',datetime('now'),datetime('now','+1 day'))")
    c.execute("INSERT INTO campaign_recipients (campaign_id,contact_id,status) VALUES (1,100,'sent')")
    c.commit()
    with _patch_db(p):
        # campaign 1 itself doesn't conflict with its own contacts
        assert cl.find_conflicts(1, [100]) == []
        # a different campaign does
        assert len(cl.find_conflicts(2, [100])) == 1


def test_confirm_elapsed_lock_requires_post_dispatch_pull(tmp_path):
    p = str(tmp_path / "s.sqlite"); c = _make_db(p)
    # window elapsed, but last pull was BEFORE dispatch -> not confirmed
    c.execute("INSERT INTO campaigns (id,name,status,dispatched_at,lock_until) "
              "VALUES (1,'A','dispatched','2026-06-10T00:00:00','2026-06-10T01:00:00')")
    c.execute("INSERT INTO sync_state (key,value) VALUES ('last_pull_ok_at','2026-06-09T00:00:00')")
    c.commit()
    with _patch_db(p):
        assert cl.confirm_elapsed_locks() == 0
    # now a pull happened after dispatch -> confirmed
    c.execute("UPDATE sync_state SET value='2026-06-11T00:00:00' WHERE key='last_pull_ok_at'")
    c.commit()
    with _patch_db(p):
        assert cl.confirm_elapsed_locks() == 1
    conn = sqlite3.connect(p)
    assert conn.execute("SELECT unsubs_confirmed_at FROM campaigns WHERE id=1").fetchone()[0] is not None
