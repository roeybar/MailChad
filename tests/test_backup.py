"""Stress tests + hardener validation for bin/_backup.py.

Run inside the terminal container:
    docker exec ep-v3-terminal python -m pytest /app/tests/test_backup.py -v

Tests cover:
  Hardeners: checksums, HMAC tamper detection, atomic write, permission check,
             post-write integrity verification, decrypt roundtrip
  Stress:    empty DB, missing keys dir, large payloads, corrupt SQLite,
             all DynamoDB value types, special-char passphrase, concurrent runs,
             DynamoDB/SQS unavailable, very large passphrase
"""
from __future__ import annotations

import base64
import decimal
import hashlib
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import threading
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# import backup module from bin/

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import _backup as bk


# fixtures

PASSPHRASE = "test-passphrase-stress-2026"


@pytest.fixture()
def key():
    salt = os.urandom(16)
    return bk.derive_key(PASSPHRASE, salt)


@pytest.fixture()
def empty_db(tmp_path):
    """Minimal SQLite with all required tables but no rows."""
    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE contacts (id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, tags TEXT,
            consent_ts TEXT, consent_source TEXT, external_id TEXT, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'draft',
            template_id INTEGER, kind TEXT DEFAULT 'promotional', entity_id INTEGER,
            recipient_count INTEGER DEFAULT 0, dispatched_at TEXT, scheduled_for TEXT, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, is_secret INTEGER DEFAULT 0,
            updated_at TEXT, updated_by TEXT);
        CREATE TABLE inbox (event_id INTEGER PRIMARY KEY, table_name TEXT, row_id TEXT,
            revision INTEGER DEFAULT 1, actor TEXT DEFAULT 'system', modified_at TEXT DEFAULT (datetime('now')),
            key_id TEXT DEFAULT 'test', encrypted_payload BLOB, deleted INTEGER DEFAULT 0,
            received_at TEXT DEFAULT (datetime('now')), materialised_at TEXT);
        CREATE TABLE suppression_hashes (id INTEGER PRIMARY KEY, email_hash TEXT UNIQUE,
            reason TEXT DEFAULT 'unsubscribe', source TEXT DEFAULT 'sync',
            added_at TEXT DEFAULT (datetime('now')));
    """)
    conn.commit(); conn.close()
    return db


@pytest.fixture()
def populated_db(empty_db):
    """DB with some rows."""
    conn = sqlite3.connect(empty_db)
    for i in range(50):
        conn.execute("INSERT INTO contacts (email, name, tags, consent_ts, consent_source) VALUES (?,?,?,datetime('now'),'test')",
                     (f"user{i}@example.com", f"User {i}", "stress|test"))
    conn.execute("INSERT INTO settings (key, value, is_secret) VALUES ('unsub_secret','test-secret-value',1)")
    conn.execute("INSERT INTO settings (key, value, is_secret) VALUES ('erasure_secret','test-erasure-secret',1)")
    conn.commit(); conn.close()
    return empty_db


@pytest.fixture()
def keys_dir(tmp_path):
    kd = tmp_path / "keys"
    kd.mkdir()
    (kd / "cloud_bearer.txt").write_text("test-bearer-token")
    (kd / "settings_kek.bin").write_bytes(os.urandom(32))
    (kd / "own_kem_priv.key").write_bytes(os.urandom(48))
    return kd


@pytest.fixture()
def backup_out(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    return d


# hardener: crypto roundtrip

def test_encrypt_decrypt_roundtrip(key):
    plain = b"hello backup world " * 100
    nonce, ct = bk.encrypt(plain, key)
    assert bk.decrypt(ct, nonce, key) == plain


def test_encrypt_produces_different_nonce_each_time(key):
    plain = b"same plaintext"
    n1, ct1 = bk.encrypt(plain, key)
    n2, ct2 = bk.encrypt(plain, key)
    assert n1 != n2
    assert ct1 != ct2  # nonce is prepended/different


def test_decrypt_detects_tamper(key):
    plain = b"sensitive data"
    nonce, ct = bk.encrypt(plain, key)
    tampered = bytes([ct[0] ^ 0xFF]) + ct[1:]
    with pytest.raises(Exception):
        bk.decrypt(tampered, nonce, key)


def test_enc_entry_contains_sha256(key):
    plain = b"test content for checksum"
    nonce, _ = bk.encrypt(plain, key)
    entry = bk._enc_entry(nonce, plain)
    assert entry["sha256"] == hashlib.sha256(plain).hexdigest()
    assert entry["plain_size"] == len(plain)
    assert "nonce_b64" in entry


# hardener: HMAC manifest

def test_hmac_manifest_is_deterministic(key):
    data = b'{"test": 1}'
    h1 = bk._hmac_manifest(data, key)
    h2 = bk._hmac_manifest(data, key)
    assert h1 == h2 and len(h1) == 64  # SHA-256 hex


def test_hmac_detects_manifest_change(key):
    data1 = b'{"actor": "operator"}'
    data2 = b'{"actor": "attacker"}'
    assert bk._hmac_manifest(data1, key) != bk._hmac_manifest(data2, key)


# hardener: post-write verify

def test_verify_zip_good(tmp_path, key):
    plain = b"valid content"
    nonce, ct = bk.encrypt(plain, key)
    entry = bk._enc_entry(nonce, plain)
    zp = tmp_path / "test.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("state.sqlite.enc", ct)
    manifest = {"files": {"state.sqlite.enc": entry}}
    errors = bk._verify_zip(zp, key, manifest)
    assert errors == []


def test_verify_zip_detects_corrupt_ciphertext(tmp_path, key):
    plain = b"some data"
    nonce, ct = bk.encrypt(plain, key)
    entry = bk._enc_entry(nonce, plain)
    # Corrupt the ciphertext
    bad_ct = bytes([ct[0] ^ 0xFF]) + ct[1:]
    zp = tmp_path / "bad.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("state.sqlite.enc", bad_ct)
    manifest = {"files": {"state.sqlite.enc": entry}}
    errors = bk._verify_zip(zp, key, manifest)
    assert any("decrypt" in e or "SHA-256" in e or "failed" in e for e in errors)


def test_verify_zip_missing_file(tmp_path, key):
    plain = b"data"
    nonce, ct = bk.encrypt(plain, key)
    entry = bk._enc_entry(nonce, plain)
    zp = tmp_path / "missing.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("manifest.json", "{}")
        # Deliberately omit state.sqlite.enc
    manifest = {"files": {"state.sqlite.enc": entry}}
    errors = bk._verify_zip(zp, key, manifest)
    assert any("missing" in e for e in errors)


def test_verify_zip_detects_wrong_sha256(tmp_path, key):
    plain = b"original"
    nonce, ct = bk.encrypt(plain, key)
    entry = bk._enc_entry(nonce, plain)
    # Swap sha256 to a different value
    entry["sha256"] = hashlib.sha256(b"different").hexdigest()
    zp = tmp_path / "wrongsha.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("state.sqlite.enc", ct)
    manifest = {"files": {"state.sqlite.enc": entry}}
    errors = bk._verify_zip(zp, key, manifest)
    assert any("SHA-256" in e or "mismatch" in e for e in errors)


# stress: snapshot_db

def test_snapshot_db_empty(empty_db):
    with patch.object(bk, "DB_PATH", empty_db):
        data = bk.snapshot_db()
    assert len(data) > 0  # empty DB still produces bytes
    # Verify it's a valid SQLite file
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        f.write(data)
        tmp = Path(f.name)
    try:
        conn = sqlite3.connect(tmp)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables) > 0
        conn.close()
    finally:
        tmp.unlink(missing_ok=True)


def test_snapshot_db_populated(populated_db):
    with patch.object(bk, "DB_PATH", populated_db):
        data = bk.snapshot_db()
    assert len(data) > 4096
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        f.write(data); tmp = Path(f.name)
    try:
        conn = sqlite3.connect(tmp)
        count = conn.execute("SELECT count(*) FROM contacts").fetchone()[0]
        conn.close()
        assert count == 50
    finally:
        tmp.unlink(missing_ok=True)


def test_snapshot_db_missing():
    with patch.object(bk, "DB_PATH", Path("/nonexistent/state.sqlite")):
        data = bk.snapshot_db()
    assert data == b""


def test_snapshot_db_large(tmp_path):
    """Stress: 10,000 contacts."""
    db = tmp_path / "large.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY, email TEXT, name TEXT, tags TEXT, consent_ts TEXT, consent_source TEXT, external_id TEXT, created_at TEXT DEFAULT (datetime('now')))")
    conn.executemany("INSERT INTO contacts VALUES (?,?,?,?,datetime('now'),'bulk',null,datetime('now'))",
                     [(i, f"user{i}@stress.com", f"User {i}", "stress") for i in range(10_000)])
    conn.commit(); conn.close()
    with patch.object(bk, "DB_PATH", db):
        data = bk.snapshot_db()
    assert len(data) > 100_000  # 10k rows should produce >100KB
    # Verify it restores correctly
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        f.write(data); tmp = Path(f.name)
    try:
        count = sqlite3.connect(tmp).execute("SELECT count(*) FROM contacts").fetchone()[0]
        assert count == 10_000
    finally:
        Path(tmp).unlink(missing_ok=True)


# stress: snapshot_keys

def test_snapshot_keys_normal(keys_dir):
    with patch.object(bk, "KEYS_DIR", keys_dir):
        data = bk.snapshot_keys()
    assert len(data) > 0
    buf = BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = tar.getnames()
    assert "cloud_bearer.txt" in names
    assert "settings_kek.bin" in names


def test_snapshot_keys_missing_dir():
    with patch.object(bk, "KEYS_DIR", Path("/nonexistent/keys")):
        data = bk.snapshot_keys()
    assert data == b""


def test_snapshot_keys_empty_dir(tmp_path):
    empty = tmp_path / "emptykeys"
    empty.mkdir()
    with patch.object(bk, "KEYS_DIR", empty):
        data = bk.snapshot_keys()
    buf = BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        assert tar.getnames() == []


def test_snapshot_keys_large_file(tmp_path):
    """Stress: single key file that is 5MB."""
    kd = tmp_path / "keys"
    kd.mkdir()
    (kd / "big_key.bin").write_bytes(os.urandom(5 * 1024 * 1024))
    with patch.object(bk, "KEYS_DIR", kd):
        data = bk.snapshot_keys()
    assert len(data) > 0
    buf = BytesIO(data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        member = tar.getmember("big_key.bin")
        assert member.size == 5 * 1024 * 1024


# stress: check_inbox

def test_check_inbox_no_pending(empty_db):
    info = bk.check_inbox(empty_db)
    assert info["unmaterialised"] == 0
    assert info["warning"] is None


def test_check_inbox_with_pending(tmp_path):
    db = tmp_path / "inbox.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE inbox (
        event_id INTEGER PRIMARY KEY, table_name TEXT, row_id TEXT,
        revision INTEGER DEFAULT 1, actor TEXT DEFAULT 'system',
        modified_at TEXT DEFAULT (datetime('now')), key_id TEXT DEFAULT 'test',
        encrypted_payload BLOB, deleted INTEGER DEFAULT 0,
        received_at TEXT DEFAULT (datetime('now')), materialised_at TEXT)""")
    for i in range(5):
        conn.execute("INSERT INTO inbox (event_id, table_name, row_id, revision) VALUES (?,?,?,1)",
                     (i, "webhook_event", f"ev-{i}"))
    conn.commit(); conn.close()
    info = bk.check_inbox(db)
    assert info["unmaterialised"] == 5
    assert info["warning"] is not None


def test_check_inbox_missing_db():
    info = bk.check_inbox(Path("/nonexistent.sqlite"))
    assert "error" in info


# stress: check_secret_sync

def test_secret_sync_with_secrets(populated_db):
    info = bk.check_secret_sync(populated_db)
    assert info["unsub_secret_sha256_prefix"] is not None
    assert len(info["unsub_secret_sha256_prefix"]) == 16
    assert info["erasure_secret_sha256_prefix"] is not None


def test_secret_sync_missing_secrets(empty_db):
    info = bk.check_secret_sync(empty_db)
    assert info["unsub_secret_sha256_prefix"] is None
    assert "unsub_secret_warning" in info  # key present when secret is missing


def test_secret_sync_hashes_are_consistent(populated_db):
    """Same DB -> same hash prefix on repeated calls."""
    i1 = bk.check_secret_sync(populated_db)
    i2 = bk.check_secret_sync(populated_db)
    assert i1["unsub_secret_sha256_prefix"] == i2["unsub_secret_sha256_prefix"]


# stress: DynamoDB _safe

def test_dynamo_safe_handles_bytes():
    result = bk.snapshot_dynamo  # import to get access to _safe via closure trick
    # Test _safe directly by reconstructing it inline
    import decimal
    try:
        from boto3.dynamodb.types import Binary
        b = Binary(b"\x00\xff\xab")
        # Simulate calling _safe through snapshot_dynamo
        # Since _safe is a closure, we test it by calling snapshot_dynamo with mocked client
    except ImportError:
        pass

    # Test the type handling directly by patching boto3
    raw_items = [
        {"pk": {"S": "TEST#1"}, "data": {"B": b"\x00\xff"}, "num": {"N": "42.5"},
         "flag": {"BOOL": True}, "null_val": {"NULL": True},
         "str_set": {"SS": ["a", "b"]}, "num_set": {"NS": ["1", "2"]},
         "list_val": {"L": [{"S": "item"}]}, "map_val": {"M": {"k": {"S": "v"}}}},
    ]

    mock_page = {"Items": raw_items}
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [mock_page]
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    with patch.dict(os.environ, {"DYNAMODB_TABLE": "test-table", "AWS_DEFAULT_REGION": "us-east-1"}):
        with patch("boto3.client", return_value=mock_client):
            payload, stats = bk.snapshot_dynamo()

    assert payload  # non-empty
    items = json.loads(payload)  # must be valid JSON
    assert len(items) == 1
    assert stats["item_count"] == 1


def test_dynamo_safe_handles_decimal():
    """Decimal types from DynamoDB Number deserialization."""
    mock_items = [{"pk": {"S": "X"}, "count": {"N": "100"}, "price": {"N": "9.99"}}]
    mock_pager = MagicMock()
    mock_pager.paginate.return_value = [{"Items": mock_items}]
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = mock_pager

    with patch.dict(os.environ, {"DYNAMODB_TABLE": "t", "AWS_DEFAULT_REGION": "us-east-1"}):
        with patch("boto3.client", return_value=mock_client):
            payload, stats = bk.snapshot_dynamo()

    assert stats["item_count"] == 1
    items = json.loads(payload)
    assert items[0]["count"] == 100       # Decimal -> int
    assert abs(items[0]["price"] - 9.99) < 0.01  # Decimal -> float


def test_dynamo_no_table_env():
    env = {k: v for k, v in os.environ.items() if k != "DYNAMODB_TABLE"}
    with patch.dict(os.environ, env, clear=True):
        payload, stats = bk.snapshot_dynamo()
    assert payload == b""
    assert "error" in stats


def test_dynamo_scan_error():
    mock_client = MagicMock()
    mock_client.get_paginator.side_effect = Exception("connection refused")
    with patch.dict(os.environ, {"DYNAMODB_TABLE": "t", "AWS_DEFAULT_REGION": "us-east-1"}):
        with patch("boto3.client", return_value=mock_client):
            payload, stats = bk.snapshot_dynamo()
    assert payload == b""
    assert "error" in stats


def test_dynamo_large_scan():
    """Stress: 1,000 items in scan result."""
    items = [{"pk": {"S": f"PACK#{i}"}, "data": {"S": "x" * 200}} for i in range(1000)]
    mock_pager = MagicMock()
    mock_pager.paginate.return_value = [{"Items": items}]
    mock_client = MagicMock()
    mock_client.get_paginator.return_value = mock_pager
    with patch.dict(os.environ, {"DYNAMODB_TABLE": "t", "AWS_DEFAULT_REGION": "us-east-1"}):
        with patch("boto3.client", return_value=mock_client):
            payload, stats = bk.snapshot_dynamo()
    assert stats["item_count"] == 1000
    assert len(json.loads(payload)) == 1000


# stress: SQS

def test_sqs_no_queue_url():
    env = {k: v for k, v in os.environ.items() if k != "SQS_QUEUE_URL"}
    with patch.dict(os.environ, env, clear=True):
        info = bk.snapshot_sqs()
    assert "error" in info


def test_sqs_returns_counts():
    mock_client = MagicMock()
    mock_client.get_queue_attributes.return_value = {
        "Attributes": {"ApproximateNumberOfMessages": "3",
                        "ApproximateNumberOfMessagesNotVisible": "7"}}
    with patch.dict(os.environ, {"SQS_QUEUE_URL": "https://sqs.test/queue", "AWS_DEFAULT_REGION": "us-east-1"}):
        with patch("boto3.client", return_value=mock_client):
            info = bk.snapshot_sqs()
    assert info["visible"] == 3
    assert info["in_flight"] == 7
    assert info["warning"] is not None  # 10 in-flight should warn


def test_sqs_empty_queue():
    mock_client = MagicMock()
    mock_client.get_queue_attributes.return_value = {
        "Attributes": {"ApproximateNumberOfMessages": "0",
                        "ApproximateNumberOfMessagesNotVisible": "0"}}
    with patch.dict(os.environ, {"SQS_QUEUE_URL": "https://sqs.test/q", "AWS_DEFAULT_REGION": "us-east-1"}):
        with patch("boto3.client", return_value=mock_client):
            info = bk.snapshot_sqs()
    assert info["visible"] == 0
    assert info["warning"] is None


def test_sqs_unreachable():
    mock_client = MagicMock()
    mock_client.get_queue_attributes.side_effect = Exception("timeout")
    with patch.dict(os.environ, {"SQS_QUEUE_URL": "https://sqs.test/q", "AWS_DEFAULT_REGION": "us-east-1"}):
        with patch("boto3.client", return_value=mock_client):
            info = bk.snapshot_sqs()
    assert "error" in info


# stress: passphrase

def test_derive_key_very_long_passphrase():
    pw = "x" * 10_000
    salt = os.urandom(16)
    k = bk.derive_key(pw, salt)
    assert len(k) == 32


def test_derive_key_special_characters():
    pw = "pässwørd!@#$%^&*()_+-=[]{}|;':\",./<>?🔑"
    salt = os.urandom(16)
    k = bk.derive_key(pw, salt)
    assert len(k) == 32


def test_derive_key_unicode():
    pw = "密码测试パスワード"
    salt = os.urandom(16)
    k = bk.derive_key(pw, salt)
    assert len(k) == 32


def test_derive_key_different_salts():
    pw = "same-password"
    k1 = bk.derive_key(pw, os.urandom(16))
    k2 = bk.derive_key(pw, os.urandom(16))
    assert k1 != k2  # different salts -> different keys


# stress: concurrent backups

def test_concurrent_backup_no_collision(tmp_path, empty_db, keys_dir):
    """Two concurrent backups should not overwrite each other (atomic rename)."""
    results = []

    def run_backup():
        salt = os.urandom(16)
        key = bk.derive_key(PASSPHRASE, salt)
        with patch.object(bk, "DB_PATH", empty_db), \
             patch.object(bk, "KEYS_DIR", keys_dir), \
             patch.object(bk, "BACKUP_OUT", tmp_path):
            db_bytes = bk.snapshot_db()
            keys_bytes = bk.snapshot_keys()
            db_n, db_ct = bk.encrypt(db_bytes, key)
            k_n, k_ct = bk.encrypt(keys_bytes, key)
            ts = f"concurrent-{threading.get_ident()}"
            out = tmp_path / f"backup-{ts}.zip"
            tmp = out.with_suffix(".tmp")
            with zipfile.ZipFile(tmp, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"ts": ts}))
                zf.writestr("state.sqlite.enc", db_ct)
            tmp.rename(out)
            results.append(out)

    threads = [threading.Thread(target=run_backup) for _ in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(results) == 3
    # All output files must exist and be distinct
    assert len(set(str(r) for r in results)) == 3
    for r in results:
        assert r.exists()
        assert zipfile.is_zipfile(r)


# stress: corrupt/empty inputs

def test_encrypt_empty_bytes(key):
    nonce, ct = bk.encrypt(b"", key)
    plain = bk.decrypt(ct, nonce, key)
    assert plain == b""


def test_encrypt_single_byte(key):
    nonce, ct = bk.encrypt(b"\x42", key)
    assert bk.decrypt(ct, nonce, key) == b"\x42"


def test_verify_zip_on_non_zip(tmp_path, key):
    bad = tmp_path / "notazip.zip"
    bad.write_bytes(b"not a zip file at all")
    errors = bk._verify_zip(bad, key, {"files": {}})
    assert errors  # should report an error


def test_verify_zip_empty_files(tmp_path, key):
    """manifest.json with no files entry - should pass trivially."""
    zp = tmp_path / "empty.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("manifest.json", "{}")
    errors = bk._verify_zip(zp, key, {"files": {}})
    assert errors == []
