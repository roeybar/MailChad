"""Tests for webhook receiver - §4.3, §10.4."""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import nacl.public
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent

WEBHOOK_SECRET = "test-webhook-secret-base64string=="


def _sign(svix_id: str, svix_ts: str, body: bytes) -> str:
    signed = f"{svix_id}.{svix_ts}.{body.decode()}".encode()
    sig = base64.b64encode(hmac.new(WEBHOOK_SECRET.encode(), signed, hashlib.sha256).digest()).decode()
    return f"v1,{sig}"


@pytest.fixture
def client(tmp_cloud_dynamo, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "tt")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("DISABLE_DISPATCHER", "1")
    # tmp_cloud_dynamo already sets sys.path + calls dynamo.init()
    from mailchad.cloud.main import app
    c = TestClient(app)
    # Register both pubkeys (needed for encrypt-to-both)
    for role in ("operator", "client"):
        p = nacl.public.PrivateKey.generate()
        c.post("/init/handshake",
            json={"role": role, "kem_pub": base64.b64encode(bytes(p.public_key)).decode()},
            headers={"X-Bootstrap-Token": "tt"})
    return c


def test_unsigned_webhook_rejected(client):
    body = json.dumps({"type": "email.sent", "data": {"id": "abc"}}).encode()
    r = client.post("/webhooks/resend", content=body)
    assert r.status_code == 400   # missing svix-* headers


def test_bad_signature_rejected(client):
    body = json.dumps({"type": "email.sent", "data": {"id": "abc"}}).encode()
    r = client.post("/webhooks/resend", content=body, headers={
        "svix-id": "evt_1", "svix-timestamp": str(int(time.time())),
        "svix-signature": "v1,wrongsignature==",
    })
    assert r.status_code == 401


def test_replay_window_enforced(client):
    body = json.dumps({"type": "email.sent", "data": {"id": "abc"}}).encode()
    old_ts = str(int(time.time()) - 9999)
    r = client.post("/webhooks/resend", content=body, headers={
        "svix-id": "evt_2", "svix-timestamp": old_ts,
        "svix-signature": _sign("evt_2", old_ts, body),
    })
    assert r.status_code == 400


def test_valid_webhook_accepted(client):
    body = json.dumps({"type": "email.sent", "data": {"id": "abc123", "to": ["x@example.com"]}}).encode()
    ts = str(int(time.time()))
    r = client.post("/webhooks/resend", content=body, headers={
        "svix-id": "evt_3", "svix-timestamp": ts,
        "svix-signature": _sign("evt_3", ts, body),
    })
    assert r.status_code == 200
    body_j = r.json()
    assert body_j["ok"] is True
    assert "forwarded_event_id" in body_j


def test_duplicate_svix_id_idempotent(client):
    body = json.dumps({"type": "email.sent", "data": {"id": "x", "to": ["x@x.com"]}}).encode()
    ts = str(int(time.time()))
    h = {"svix-id": "evt_dup", "svix-timestamp": ts,
         "svix-signature": _sign("evt_dup", ts, body)}
    r1 = client.post("/webhooks/resend", content=body, headers=h)
    r2 = client.post("/webhooks/resend", content=body, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("duplicate") is True


def test_bounce_triggers_suppression(client):
    body = json.dumps({"type": "email.bounced",
                       "data": {"id": "bounce1", "to": ["bouncey@x.com"]}}).encode()
    ts = str(int(time.time()))
    r = client.post("/webhooks/resend", content=body, headers={
        "svix-id": "evt_bounce", "svix-timestamp": ts,
        "svix-signature": _sign("evt_bounce", ts, body),
    })
    assert r.status_code == 200
    assert r.json()["suppression"] == "bounce_hard"


def test_complaint_triggers_suppression(client):
    body = json.dumps({"type": "email.complained",
                       "data": {"id": "c1", "to": ["spammer@x.com"]}}).encode()
    ts = str(int(time.time()))
    r = client.post("/webhooks/resend", content=body, headers={
        "svix-id": "evt_comp", "svix-timestamp": ts,
        "svix-signature": _sign("evt_comp", ts, body),
    })
    assert r.json()["suppression"] == "complaint"
