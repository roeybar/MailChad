"""legacy_wg_snapshot and legacy_wg_sync scenarios run as direct calls on a
db.init() DB seeded by tests/fixtures/orm_levels.py.

The literal expected values are pinned in this file.
SQL datetime('now') values cannot be pinned; they compare as NOW after a recency check.
"""
from __future__ import annotations

import asyncio
import base64
import calendar
import gzip
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import orm_levels  # noqa: E402

NOW = "<now>"
_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
KEY = bytes(range(32))
CLOCK = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
SNAPSHOT_ID = "b97f69f7-5edf-45"   # snapshot::create_and_push (uuid4 drawn from random.Random(1234))

# Seeded rows the scenarios touch.
SH0 = {"added_at": NOW, "email_hash": "suppression_hashes-email_hash-0", "reason": "unsubscribe", "scope": "all",
       "source": None}
SH1 = {"added_at": NOW, "email_hash": "suppression_hashes-email_hash-1", "reason": "complaint", "scope": "all",
       "source": "suppression_hashes-source-1"}
CR1 = {"batch_no": None, "bounced_at": None, "campaign_id": 1, "clicked_at": None, "complained_at": None,
       "contact_id": 1, "delivered_at": None, "failure_reason": None, "id": 1, "is_seed": 0, "message_id": None,
       "opened_at": None, "sent_at": None, "status": "queued"}
CR2 = {"batch_no": 2, "bounced_at": "campaign_recipients-bounced_at-1", "campaign_id": 2,
       "clicked_at": "campaign_recipients-clicked_at-1", "complained_at": "campaign_recipients-complained_at-1",
       "contact_id": 2, "delivered_at": "campaign_recipients-delivered_at-1",
       "failure_reason": "campaign_recipients-failure_reason-1", "id": 2, "is_seed": 0,
       "message_id": "campaign_recipients-message_id-1", "opened_at": "campaign_recipients-opened_at-1",
       "sent_at": "campaign_recipients-sent_at-1", "status": "sent"}
DJ0 = {"acked_at": None, "campaign_id": 1, "completed_at": None, "content_hash": "dispatched_job-content_hash-0",
       "dispatched_at": NOW, "job_id": "dispatched_job-job_id-0", "recipient_hash": "dispatched_job-recipient_hash-0",
       "recipient_id": 1, "resend_message_id": None, "status": "dispatched"}

# sync EVENTS and the two events rows every ingest_events scenario adds (ids 3, 4).
EVENTS = [{"id": 1, "event_type": "email.delivered", "message_id": "campaign_recipients-message_id-1",
           "recipient": "r@example.com", "payload_json": "{}"},
          {"id": 2, "event_type": "email.unknown", "message_id": None, "recipient": None, "payload_json": "{}"}]
EV3 = {"event_type": "email.delivered", "id": 3, "message_id": "campaign_recipients-message_id-1",
       "payload_json": "{}", "received_at": NOW, "recipient": "r@example.com", "synced_from_edge_at": NOW}
EV4 = {"event_type": "email.unknown", "id": 4, "message_id": None, "payload_json": "{}", "received_at": NOW,
       "recipient": None, "synced_from_edge_at": NOW}
EVENTS_SEQ = {"-": [{"name": "events", "seq": 2}], "+": [{"name": "events", "seq": 4}]}


def _drift(i, category, severity, job_id, details):
    return {"acknowledged_at": None, "acknowledged_by": None, "category": category,
            "details_json": json.dumps(details), "detected_at": NOW, "id": i, "job_id": job_id, "severity": severity}


def _norm(v):
    if isinstance(v, str) and _TS.match(v):
        age = time.time() - calendar.timegm(time.strptime(v, "%Y-%m-%d %H:%M:%S"))
        assert -5 <= age <= 600, v
        return NOW
    return v


def _dump(path) -> dict:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    try:
        return {t: [{k: _norm(r[k]) for k in r.keys()} for r in c.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()}
    finally:
        c.close()


def _schema(path) -> list:
    c = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    finally:
        c.close()


def _delta(before: dict, after: dict) -> dict:
    out = {}
    for t in sorted(set(before) | set(after)):
        a, b = before.get(t, []), after.get(t, [])
        removed, added = [r for r in a if r not in b], [r for r in b if r not in a]
        if removed or added:
            out[t] = {"-": removed, "+": added}
    return out


def _sql(path, stmt) -> None:
    c = sqlite3.connect(str(path))
    try:
        c.execute(stmt)
        c.commit()
    finally:
        c.close()


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return CLOCK if tz else CLOCK.replace(tzinfo=None)


@pytest.fixture
def wg(tmp_path, monkeypatch):
    """A db.init() DB plus the orm_levels seeder; returns (db path, the dump before the scenario)."""
    from mailman.terminal import db
    path = tmp_path / "state.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    c = sqlite3.connect(str(path))
    try:
        c.execute("PRAGMA foreign_keys = ON")
        orm_levels.seed_db(c)
        c.commit()
    finally:
        c.close()
    return path, _dump(path)


def _http(monkeypatch, handler) -> list:
    """Every httpx.AsyncClient of the module answers through handler; returns the request log."""
    log = []
    real = httpx.AsyncClient

    def handle(request):
        log.append(request)
        return handler(request)

    class Client(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handle)
            super().__init__(*a, **k)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return log


def _ok(request):
    return httpx.Response(200, json={"ok": True})


def _fail(request):
    return httpx.Response(503, json={"error": "down"})


def _snap(monkeypatch):
    from mailman.terminal import legacy_wg_snapshot
    monkeypatch.setenv("DB_ENCRYPTION_KEY", base64.b64encode(KEY).decode())
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(bytes=random.Random(1234).randbytes(16), version=4))
    monkeypatch.setattr(legacy_wg_snapshot, "datetime", _Frozen)
    return legacy_wg_snapshot


def _sync():
    from mailman.terminal import legacy_wg_sync
    return legacy_wg_sync


# legacy_wg_snapshot ----------------------------------------------------------

def test_mirror_rows(wg, monkeypatch):
    # snapshot::mirror_rows
    path, before = wg
    m = _snap(monkeypatch)
    log = _http(monkeypatch, _ok)
    assert asyncio.run(m.sync_suppression_mirror()) == {"synced": 2, "ack": {"ok": True}}
    assert [(r.method, str(r.url)) for r in log] == [("POST", "http://172.31.0.4:8001/suppression-mirror/sync")]
    assert [{k: _norm(v) for k, v in row.items()} for row in json.loads(log[0].content)] == [
        {"added_at": NOW, "email_hash": "suppression_hashes-email_hash-0", "reason": "unsubscribe"},
        {"added_at": NOW, "email_hash": "suppression_hashes-email_hash-1", "reason": "complaint"}]
    assert _delta(before, _dump(path)) == {}


def test_mirror_rows_rejected(wg, monkeypatch):
    # snapshot::mirror_rows_rejected
    path, before = wg
    m = _snap(monkeypatch)
    log = _http(monkeypatch, _fail)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(m.sync_suppression_mirror())
    assert str(ei.value) == ("Server error '503 Service Unavailable' for url "
                             "'http://172.31.0.4:8001/suppression-mirror/sync'\n"
                             "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503")
    assert len(log) == 1
    assert _delta(before, _dump(path)) == {}


def test_mirror_empty(wg, monkeypatch):
    # snapshot::mirror_empty
    path, before = wg
    _sql(path, "DELETE FROM suppression_hashes")
    m = _snap(monkeypatch)
    log = _http(monkeypatch, _ok)
    assert asyncio.run(m.sync_suppression_mirror()) == {"synced": 0}
    assert log == []
    assert _delta(before, _dump(path)) == {"suppression_hashes": {"-": [SH0, SH1], "+": []}}


def test_dump_db(wg, monkeypatch):
    # snapshot::dump_db: the snapshot holds the source's schema and every row
    path, before = wg
    m = _snap(monkeypatch)
    snap = path.parent / "snap.sqlite"
    snap.write_bytes(gzip.decompress(m._dump_db()))
    assert _schema(snap) == _schema(path)
    assert _dump(snap) == before
    assert _dump(snap)["suppression_hashes"] == [SH0, SH1]
    assert _delta(before, _dump(path)) == {}


def test_dump_db_keeps_wal_rows_and_no_temp_file(wg, monkeypatch, tmp_path):
    # The online backup carries a committed row that lives only in the WAL (another connection
    # keeps it there with wal_autocheckpoint=0), and the temp copy is gone when _dump_db() returns.
    import shutil
    import tempfile
    path, _ = wg
    m = _snap(monkeypatch)
    tmpdir = tmp_path / "tempdir"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    held = sqlite3.connect(str(path))
    try:
        held.execute("PRAGMA wal_autocheckpoint=0")
        held.execute("INSERT INTO suppression_hashes (email_hash, reason, source) VALUES ('h-wal', 'manual', 'wal')")
        held.commit()
        main_only = tmp_path / "main-only.sqlite"
        shutil.copyfile(path, main_only)
        assert _dump(main_only)["suppression_hashes"] == [SH0, SH1]   # the row is not in the main file
        dumped = m._dump_db()
    finally:
        held.close()
    snap = tmp_path / "snap.sqlite"
    snap.write_bytes(gzip.decompress(dumped))
    assert _dump(snap)["suppression_hashes"] == [
        SH0, SH1, {"added_at": NOW, "email_hash": "h-wal", "reason": "manual", "scope": "all", "source": "wal"}]
    assert sorted(p.name for p in tmpdir.iterdir()) == []


def test_create_and_push(wg, monkeypatch):
    # snapshot::create_and_push. The size depends on the DB bytes, which carry SQL
    # datetime('now') values, so it is checked against the shipped body instead of a literal.
    path, before = wg
    m = _snap(monkeypatch)
    log = _http(monkeypatch, _ok)
    res = asyncio.run(m.create_and_push())
    assert [(r.method, str(r.url)) for r in log] == [("POST", "http://172.31.0.4:8001/snapshot")]
    head, _, blob = log[0].content.partition(b"\n")
    meta = json.loads(head)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    plain = AESGCM(KEY).decrypt(blob[:12], blob[12:], None)
    assert res == {"snapshot_id": SNAPSHOT_ID, "size": len(blob), "ack": {"ok": True}}
    assert meta == {"snapshot_id": SNAPSHOT_ID, "timestamp": "2026-01-02T03:04:05.123456+00:00",
                    "encryption": "aes-256-gcm", "plain_sha256": hashlib.sha256(plain).hexdigest(),
                    "plain_size": len(plain), "cipher_size": len(blob)}
    assert len(blob) == len(plain) + 28
    snap = path.parent / "shipped.sqlite"
    snap.write_bytes(gzip.decompress(plain))
    assert _dump(snap) == before
    assert _delta(before, _dump(path)) == {
        "audit_event": {"-": [], "+": [{
            "action": "snapshot.shipped", "actor": "system:vault-snapshot",
            "details_json": json.dumps({"cipher_size": len(blob), "ack": {"ok": True}}), "id": 3,
            "occurred_at": NOW, "target": f"snapshot:{SNAPSHOT_ID}"}]},
        "sqlite_sequence": {"-": [{"name": "audit_event", "seq": 2}], "+": [{"name": "audit_event", "seq": 3}]}}


def test_create_and_push_nonce_from_urandom(wg, monkeypatch):
    # Each shipped GCM nonce is the 12 bytes os.urandom returned during that call, and two
    # calls ship different nonces, so a constant, import-time or lazily cached nonce fails in any test order.
    m = _snap(monkeypatch)
    log = _http(monkeypatch, _ok)
    drawn = []
    real = os.urandom

    def spy(n):
        b = real(n)
        drawn.append(b)
        return b
    monkeypatch.setattr(os, "urandom", spy)
    nonces = []
    for call in range(2):
        start = len(drawn)
        asyncio.run(m.create_and_push())
        blob = log[call].content.partition(b"\n")[2]
        assert [b for b in drawn[start:] if len(b) == 12] == [blob[:12]], call
        nonces.append(blob[:12])
    assert nonces[0] != nonces[1]


def test_create_and_push_rejected(wg, monkeypatch):
    # snapshot::create_and_push_rejected
    path, before = wg
    m = _snap(monkeypatch)
    log = _http(monkeypatch, _fail)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(m.create_and_push())
    assert str(ei.value) == ("Server error '503 Service Unavailable' for url 'http://172.31.0.4:8001/snapshot'\n"
                             "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503")
    assert [str(r.url) for r in log] == ["http://172.31.0.4:8001/snapshot"]
    assert _delta(before, _dump(path)) == {}


# legacy_wg_sync --------------------------------------------------------------

def test_ingest_events(wg):
    # sync::ingest_events
    path, before = wg
    assert _sync()._ingest_events(EVENTS) == 2
    assert _delta(before, _dump(path)) == {
        "campaign_recipients": {"-": [CR2], "+": [dict(CR2, status="delivered", delivered_at=NOW)]},
        "events": {"-": [], "+": [EV3, EV4]},
        "sqlite_sequence": EVENTS_SEQ}


@pytest.mark.parametrize("status", ["complained", "bounced", "failed"])
def test_ingest_events_terminal_status_kept(wg, status):
    # sync::ingest_events_complained_kept, ingest_events_bounced_kept, ingest_events_failed_kept
    path, before = wg
    _sql(path, f"UPDATE campaign_recipients SET status='{status}' WHERE message_id='campaign_recipients-message_id-1'")
    assert _sync()._ingest_events(EVENTS) == 2
    assert _delta(before, _dump(path)) == {
        "campaign_recipients": {"-": [CR2], "+": [dict(CR2, status=status)]},
        "events": {"-": [], "+": [EV3, EV4]},
        "sqlite_sequence": EVENTS_SEQ}


def test_ingest_event_without_message_id_touches_no_recipient(wg):
    # A mapped event type with message_id None must not match CR1 (message_id NULL) or any recipient.
    path, before = wg
    assert CR1 in before["campaign_recipients"]
    assert _sync()._ingest_events([{"id": 9, "event_type": "email.delivered", "message_id": None,
                                    "recipient": "r@example.com", "payload_json": "{}"}]) == 1
    assert _dump(path)["campaign_recipients"] == before["campaign_recipients"]
    assert _delta(before, _dump(path)) == {
        "events": {"-": [], "+": [{"event_type": "email.delivered", "id": 3, "message_id": None, "payload_json": "{}",
                                   "received_at": NOW, "recipient": "r@example.com", "synced_from_edge_at": NOW}]},
        "sqlite_sequence": {"-": [{"name": "events", "seq": 2}], "+": [{"name": "events", "seq": 3}]}}


def test_ingest_unsubs(wg):
    # sync::ingest_unsubs (OR IGNORE keeps the first source)
    path, before = wg
    assert _sync()._ingest_unsubs([{"email_hash": "h1", "source_token": "t1"},
                                   {"email_hash": "h1", "source_token": "t2"}]) == 2
    assert _delta(before, _dump(path)) == {"suppression_hashes": {"-": [], "+": [
        {"added_at": NOW, "email_hash": "h1", "reason": "unsubscribe", "scope": "all", "source": "t1"}]}}


@pytest.mark.parametrize("email_hash, ack_id", [("h2", 7), ("suppression_hashes-email_hash-0", 8)])
def test_ingest_erasures_locks(wg, email_hash, ack_id):
    # sync::ingest_erasures, ingest_erasures_duplicate_hash. Current behaviour:
    # db.audit inside the open write transaction waits out the busy timeout and nothing is written.
    from sqlalchemy.exc import OperationalError
    path, before = wg
    with pytest.raises(OperationalError) as ei:
        _sync()._ingest_erasures([{"email_hash": email_hash, "source_token": "t", "id": ack_id}])
    assert type(ei.value.orig).__name__ == "OperationalError"
    assert str(ei.value.orig) == "database is locked"
    assert _delta(before, _dump(path)) == {}


def test_detect_and_persist_drift(wg):
    # sync::detect_and_persist_drift
    path, before = wg
    m = _sync()
    assert m._persist_drift(m._detect_drift(
        [{"job_id": "edge-only", "status": "failed", "content_hash": "x"},
         {"job_id": "dispatched_job-job_id-0", "status": "failed", "content_hash": "other"}])) == 4
    assert _delta(before, _dump(path)) == {
        "drift_report": {"-": [], "+": [
            _drift(3, "missing_on_vault", "CRITICAL", "edge-only", {"edge_status": "failed", "edge_content_hash": "x"}),
            _drift(4, "missing_on_edge", "WARN", "dispatched_job-job_id-1", {"vault_status": "acked"}),
            _drift(5, "content_mismatch", "CRITICAL", "dispatched_job-job_id-0",
                   {"vault_hash": "dispatched_job-content_hash-0", "edge_hash": "other"}),
            _drift(6, "status_mismatch", "WARN", "dispatched_job-job_id-0", {"edge": "failed", "vault": "dispatched"})]},
        "sqlite_sequence": {"-": [{"name": "drift_report", "seq": 2}], "+": [{"name": "drift_report", "seq": 6}]}}


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
def test_drift_closed_job_not_reported(wg, status):
    # sync::drift_completed_not_reported, drift_cancelled_not_reported, drift_failed_not_reported
    path, before = wg
    _sql(path, f"UPDATE dispatched_job SET status='{status}' WHERE job_id='dispatched_job-job_id-0'")
    m = _sync()
    assert m._persist_drift(m._detect_drift([])) == 1
    assert _delta(before, _dump(path)) == {
        "dispatched_job": {"-": [DJ0], "+": [dict(DJ0, status=status)]},
        "drift_report": {"-": [], "+": [
            _drift(3, "missing_on_edge", "WARN", "dispatched_job-job_id-1", {"vault_status": "acked"})]},
        "sqlite_sequence": {"-": [{"name": "drift_report", "seq": 2}], "+": [{"name": "drift_report", "seq": 3}]}}


def test_run_sync(wg, monkeypatch):
    # sync::run_sync
    path, before = wg
    m = _sync()
    monkeypatch.setattr(m, "datetime", _Frozen)
    at_ack = {}

    def edge(request):
        p = request.url.path
        if p.endswith("/cache/events"):
            return httpx.Response(200, json={"events": EVENTS, "erasures": [],
                                             "unsubs": [{"email_hash": "h-sync", "source_token": "t"}]})
        if p.endswith("/queue"):
            return httpx.Response(200, json={"jobs": [{"job_id": "edge-only", "status": "queued", "content_hash": "x"}]})
        if p.endswith("/cache/events/ack"):
            # The DB as it is when the ack goes out. It is asserted after run_sync returns,
            # because run_sync swallows any exception raised inside the ack call.
            at_ack.update(_dump(path))
        return httpx.Response(200, json={})
    log = _http(monkeypatch, edge)
    summary = {"events": 2, "unsubs": 1, "erasures": 0, "drift": 3, "drift_critical": 1}
    assert asyncio.run(m.run_sync()) == summary
    assert at_ack.get("events") == before["events"] + [EV3, EV4]
    assert {"added_at": NOW, "email_hash": "h-sync", "reason": "unsubscribe", "scope": "all",
            "source": "t"} in at_ack["suppression_hashes"]
    assert [(r.method, str(r.url)) for r in log] == [
        ("GET", "http://172.31.0.2:8443/cache/events?since_id=0"),
        ("POST", "http://172.31.0.2:8443/cache/events/ack"),
        ("GET", "http://172.31.0.2:8443/queue")]
    assert json.loads(log[1].content) == {"last_event_id": 2, "erasure_ids": [],
                                          "now_iso": "2026-01-02T03:04:05.123456+00:00"}
    assert _delta(before, _dump(path)) == {
        "audit_event": {"-": [], "+": [{"action": "sync.completed", "actor": "system:vault-sync",
                                        "details_json": json.dumps(summary), "id": 3, "occurred_at": NOW,
                                        "target": None}]},
        "campaign_recipients": {"-": [CR2], "+": [dict(CR2, status="delivered", delivered_at=NOW)]},
        "drift_report": {"-": [], "+": [
            _drift(3, "missing_on_vault", "CRITICAL", "edge-only", {"edge_status": "queued", "edge_content_hash": "x"}),
            _drift(4, "missing_on_edge", "WARN", "dispatched_job-job_id-0", {"vault_status": "dispatched"}),
            _drift(5, "missing_on_edge", "WARN", "dispatched_job-job_id-1", {"vault_status": "acked"})]},
        "events": {"-": [], "+": [EV3, EV4]},
        "sqlite_sequence": {"-": [{"name": "audit_event", "seq": 2}, {"name": "drift_report", "seq": 2},
                                  {"name": "events", "seq": 2}],
                            "+": [{"name": "audit_event", "seq": 3}, {"name": "drift_report", "seq": 5},
                                  {"name": "events", "seq": 4}]},
        "suppression_hashes": {"-": [], "+": [
            {"added_at": NOW, "email_hash": "h-sync", "reason": "unsubscribe", "scope": "all", "source": "t"}]}}


def test_run_sync_erasure_lock_sends_no_ack(wg, monkeypatch):
    # Current behaviour on the run_sync path. The erasure ingest raises "database is locked"
    # before the ack, so no erasure id is acked and the edge keeps the request for the next wake.
    from sqlalchemy.exc import OperationalError
    path, before = wg
    m = _sync()

    def edge(request):
        if request.url.path.endswith("/cache/events"):
            return httpx.Response(200, json={"events": [], "unsubs": [],
                                             "erasures": [{"email_hash": "h2", "source_token": "t", "id": 7}]})
        return httpx.Response(200, json={})
    log = _http(monkeypatch, edge)
    with pytest.raises(OperationalError) as ei:
        asyncio.run(m.run_sync())
    assert str(ei.value.orig) == "database is locked"
    assert [(r.method, str(r.url)) for r in log] == [("GET", "http://172.31.0.2:8443/cache/events?since_id=0")]
    assert _delta(before, _dump(path)) == {}
