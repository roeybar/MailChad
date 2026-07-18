"""Unit tests for the inbox materialiser (Feature 1).

Uses a per-test SQLite file + patched encryption to verify that
webhook_event and suppression inbox rows are correctly applied.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import mailchad.terminal.sync_client as _sc
from mailchad.terminal.sync_client import _materialise_inbox_batch
import mailchad.terminal.db as _db_module


def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS inbox (
            event_id        INTEGER PRIMARY KEY,
            table_name      TEXT    NOT NULL,
            row_id          TEXT    NOT NULL,
            revision        INTEGER NOT NULL DEFAULT 1,
            actor           TEXT    NOT NULL DEFAULT 'system',
            modified_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            key_id          TEXT    NOT NULL DEFAULT 'test',
            encrypted_payload BLOB,
            deleted         INTEGER NOT NULL DEFAULT 0,
            received_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            materialised_at TEXT
        );

        CREATE TABLE IF NOT EXISTS campaign_recipients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            contact_id  INTEGER NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'queued',
            message_id  TEXT,
            sent_at     TEXT,
            opened_at   TEXT,
            clicked_at  TEXT,
            bounced_at  TEXT,
            complained_at TEXT,
            failure_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_campaign_recipients_message ON campaign_recipients (message_id);

        CREATE TABLE IF NOT EXISTS contacts (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            email   TEXT NOT NULL UNIQUE
        );
        INSERT OR IGNORE INTO contacts (id, email) VALUES (1, 'test@example.com');

        CREATE TABLE IF NOT EXISTS suppression_hashes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email_hash  TEXT NOT NULL UNIQUE,
            reason      TEXT NOT NULL DEFAULT 'unsubscribe',
            source      TEXT NOT NULL DEFAULT 'sync',
            added_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def _fake_payload(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")


def _run_batch(db_path: str) -> int:
    """Run _materialise_inbox_batch with patched DB path and identity-decrypt."""
    orig = _db_module.DB_PATH
    _db_module.DB_PATH = Path(db_path)
    try:
        with patch.object(_sc.encryption, "decrypt_for_both", side_effect=lambda b, bundle: b):
            return _materialise_inbox_batch(MagicMock())
    finally:
        _db_module.DB_PATH = orig


# Tests

def test_webhook_opened_is_ignored(tmp_path):
    """Opens are NOT tracked (bot/MPP noise) - email.opened must not touch the
    recipient, but the inbox row is still marked materialised (not reprocessed)."""
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO campaign_recipients (campaign_id, contact_id, status, message_id) VALUES (1, 1, 'sent', 'msg-001')")
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'webhook_event', 'ev-1', 1, ?)",
                 (_fake_payload({"event_type": "email.opened", "message_id": "msg-001"}),))
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
    row = conn.execute("SELECT status, opened_at FROM campaign_recipients WHERE message_id='msg-001'").fetchone()
    assert row["status"] == "sent"          # unchanged - open ignored
    assert row["opened_at"] is None
    mat = conn.execute("SELECT materialised_at FROM inbox WHERE event_id=1").fetchone()
    assert mat["materialised_at"] is not None   # still materialised, won't reprocess


def test_webhook_clicked(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO campaign_recipients (campaign_id, contact_id, status, message_id) VALUES (1, 1, 'sent', 'msg-002')")
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'webhook_event', 'ev-2', 1, ?)",
                 (_fake_payload({"event_type": "email.clicked", "message_id": "msg-002"}),))
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
    row = conn.execute("SELECT status, clicked_at FROM campaign_recipients WHERE message_id='msg-002'").fetchone()
    assert row["status"] == "clicked"
    assert row["clicked_at"] is not None


def test_webhook_bounced(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO campaign_recipients (campaign_id, contact_id, status, message_id) VALUES (1, 1, 'sent', 'msg-003')")
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'webhook_event', 'ev-3', 1, ?)",
                 (_fake_payload({"event_type": "email.bounced", "message_id": "msg-003"}),))
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
    row = conn.execute("SELECT status, bounced_at FROM campaign_recipients WHERE message_id='msg-003'").fetchone()
    assert row["status"] == "bounced"
    assert row["bounced_at"] is not None


def test_webhook_complained(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO campaign_recipients (campaign_id, contact_id, status, message_id) VALUES (1, 1, 'sent', 'msg-004')")
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'webhook_event', 'ev-4', 1, ?)",
                 (_fake_payload({"event_type": "email.complained", "message_id": "msg-004"}),))
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
    row = conn.execute("SELECT status, complained_at FROM campaign_recipients WHERE message_id='msg-004'").fetchone()
    assert row["status"] == "complained"
    assert row["complained_at"] is not None


def test_webhook_unknown_message_id_skips_gracefully(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'webhook_event', 'ev-5', 1, ?)",
                 (_fake_payload({"event_type": "email.opened", "message_id": "no-such-message"}),))
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
    mat = conn.execute("SELECT materialised_at FROM inbox WHERE event_id=1").fetchone()
    assert mat["materialised_at"] is not None


def test_webhook_does_not_downgrade_bounced(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO campaign_recipients (campaign_id, contact_id, status, message_id, bounced_at) VALUES (1, 1, 'bounced', 'msg-006', datetime('now'))")
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'webhook_event', 'ev-6', 1, ?)",
                 (_fake_payload({"event_type": "email.opened", "message_id": "msg-006"}),))
    conn.commit()

    _run_batch(db_path)

    row = conn.execute("SELECT status FROM campaign_recipients WHERE message_id='msg-006'").fetchone()
    assert row["status"] == "bounced"


def test_suppression_event_inserts_hash(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'suppression', 'sup-1', 1, ?)",
                 (_fake_payload({"email_hash": "abc123", "reason": "unsubscribe", "source": "webhook", "added_at": "2026-05-30T00:00:00Z"}),))
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
    row = conn.execute("SELECT * FROM suppression_hashes WHERE email_hash='abc123'").fetchone()
    assert row is not None
    assert row["reason"] == "unsubscribe"


def test_suppression_event_idempotent(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO suppression_hashes (email_hash, reason, source) VALUES ('abc123', 'manual', 'existing')")
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'suppression', 'sup-2', 1, ?)",
                 (_fake_payload({"email_hash": "abc123", "reason": "unsubscribe", "source": "webhook"}),))
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
    count = conn.execute("SELECT count(*) FROM suppression_hashes WHERE email_hash='abc123'").fetchone()[0]
    assert count == 1


def test_null_payload_materialises_without_error(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    conn = _make_db(db_path)
    conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision, encrypted_payload) VALUES (1, 'webhook_event', 'ev-null', 1, NULL)")
    conn.commit()

    n = _run_batch(db_path)

    assert n == 1
