"""Tests for dispatcher_lambda.py - SQS record -> decrypt -> mock Resend -> mark_sent.

Uses tmp_cloud_dynamo (moto DynamoDB) + respx to mock Resend HTTP calls.
dispatcher_lambda is loaded from cloud/ root, not cloud/app/.
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
CLOUD_DIR = ROOT / "cloud"


@pytest.fixture(autouse=True)
def dispatcher_on_path(tmp_cloud_dynamo):
    """Ensure cloud/ root (for dispatcher_lambda) + cloud/ (for app.*) are on path."""
    cloud_root = str(CLOUD_DIR)
    if cloud_root not in sys.path:
        sys.path.insert(0, cloud_root)
    # Purge any cached dispatcher_lambda between tests
    for mod in list(sys.modules):
        if mod in ("dispatcher_lambda", "dispatcher_local"):
            del sys.modules[mod]
    yield
    for mod in list(sys.modules):
        if mod in ("dispatcher_lambda", "dispatcher_local"):
            del sys.modules[mod]


def _make_record(pack_id: str) -> dict:
    return {
        "body": json.dumps({"pack_id": pack_id}),
        "messageId": "msg-001",
        "receiptHandle": "rh-001",
        "attributes": {},
    }


def _push_pack(pack_id: str, payload: bytes = b'{"from":"a@b.com","recipient":"c@d.com","subject":"hi","html":"<p>hi</p>","resend_api_key":"re_test"}') -> None:
    from mailchad.cloud import dynamo, keys_dynamo
    import os
    k = os.urandom(32)
    from mailchad.cloud.encryption_cloud import encrypt_with_temp
    blob = encrypt_with_temp(payload, k)
    # Register K_temp so dispatcher can find it
    keys_dynamo.set_k_temp(k, 3600, set_by="operator")
    dynamo.put_pack(
        pack_id=pack_id, campaign_id=1,
        recipient_hash="h1", content_hash="c1",
        send_at="2026-05-28T12:00:00Z",
        key_id="K_temp_test",
        encrypted_payload=blob,
        pushed_by="operator",
    )


def test_handler_processes_valid_pack():
    from mailchad.cloud import dispatcher_lambda
    from mailchad.cloud import dynamo

    _push_pack("pack-001")

    fake_resend = MagicMock()
    fake_resend.status_code = 200
    fake_resend.json.return_value = {"id": "resend-msg-001"}

    with patch("httpx.post", return_value=fake_resend):
        result = dispatcher_lambda.handler({"Records": [_make_record("pack-001")]}, None)

    assert result["processed"] == 1
    pack = dynamo.get_pack("pack-001")
    assert pack["status"] == "sent"
    assert pack["resend_message_id"] == "resend-msg-001"
    assert pack["encrypted_payload"] is None  # wiped after send


def test_handler_idempotent_already_sent():
    from mailchad.cloud import dispatcher_lambda
    from mailchad.cloud import dynamo

    _push_pack("pack-002")
    dynamo.update_pack("pack-002", status="sent", encrypted_payload=None)

    with patch("httpx.post") as mock_post:
        result = dispatcher_lambda.handler({"Records": [_make_record("pack-002")]}, None)

    mock_post.assert_not_called()
    assert result["skipped"] == 1


def test_handler_no_k_temp_marks_stuck():
    from mailchad.cloud import dispatcher_lambda
    from mailchad.cloud import dynamo, keys_dynamo

    from mailchad.cloud import dynamo as d
    d.put_pack(
        pack_id="pack-003", campaign_id=1,
        recipient_hash="h1", content_hash="c1",
        send_at="2026-05-28T12:00:00Z",
        key_id="K_temp_missing",
        encrypted_payload=b"ciphertext",
        pushed_by="operator",
    )
    # No K_temp set
    keys_dynamo.wipe_k_temp()

    result = dispatcher_lambda.handler({"Records": [_make_record("pack-003")]}, None)

    assert result["processed"] == 1
    pack = dynamo.get_pack("pack-003")
    assert pack["status"] == "stuck_no_key"


def test_handler_resend_4xx_marks_failed():
    from mailchad.cloud import dispatcher_lambda
    from mailchad.cloud import dynamo

    _push_pack("pack-004")

    fake_resend = MagicMock()
    fake_resend.status_code = 422
    fake_resend.json.return_value = {"message": "invalid recipient"}

    with patch("httpx.post", return_value=fake_resend):
        result = dispatcher_lambda.handler({"Records": [_make_record("pack-004")]}, None)

    assert result["processed"] == 1
    pack = dynamo.get_pack("pack-004")
    assert pack["status"] == "failed"
    assert "invalid recipient" in pack["failure_reason"]


def test_handler_resend_5xx_raises_for_retry():
    """5xx -> RuntimeError so SQS visibility timeout resets and message retries."""
    from mailchad.cloud import dispatcher_lambda

    _push_pack("pack-005")

    fake_resend = MagicMock()
    fake_resend.status_code = 503
    fake_resend.json.return_value = {}

    with patch("httpx.post", return_value=fake_resend):
        with pytest.raises(RuntimeError, match="resend 503"):
            dispatcher_lambda.handler({"Records": [_make_record("pack-005")]}, None)


def test_handler_bad_sqs_record_counts_failed():
    from mailchad.cloud import dispatcher_lambda

    result = dispatcher_lambda.handler({"Records": [{"body": "not-json"}]}, None)
    assert result["failed"] == 1


def test_handler_sent_pack_writes_sync_event():
    """Successful send -> event_log entry so terminals see the status update."""
    from mailchad.cloud import dispatcher_lambda
    from mailchad.cloud import dynamo

    _push_pack("pack-006")

    fake_resend = MagicMock()
    fake_resend.status_code = 200
    fake_resend.json.return_value = {"id": "resend-msg-006"}

    with patch("httpx.post", return_value=fake_resend):
        dispatcher_lambda.handler({"Records": [_make_record("pack-006")]}, None)

    events = dynamo.query_events_since(0, limit=500)
    pack_events = [e for e in events if e["table_name"] == "pack" and e["row_id"] == "pack-006"]
    assert len(pack_events) == 1


def test_packs_push_enqueues_to_sqs(tmp_cloud_dynamo, monkeypatch):
    """POST /packs -> DynamoDB + SQS send_message called once per new pack."""
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "test-token")
    monkeypatch.setenv("SQS_QUEUE_URL", "http://localhost:4566/000000000000/ep-send-queue")
    monkeypatch.setenv("DISABLE_DISPATCHER", "1")

    from mailchad.cloud.main import app
    from fastapi.testclient import TestClient
    import nacl.public
    from moto import mock_aws
    import boto3

    c = TestClient(app)
    op_priv = nacl.public.PrivateKey.generate()
    h = {"X-Bootstrap-Token": "test-token"}
    bearer = c.post("/init/handshake",
        json={"role": "operator", "kem_pub": base64.b64encode(bytes(op_priv.public_key)).decode()},
        headers=h).json()["bearer"]

    sent_messages = []

    def fake_send_message(**kwargs):
        sent_messages.append(json.loads(kwargs["MessageBody"]))
        return {"MessageId": "fake-msg-id"}

    with patch("mailchad.cloud.packs._sqs") as mock_sqs_factory:
        mock_client = MagicMock()
        mock_client.send_message.side_effect = fake_send_message
        mock_sqs_factory.return_value = mock_client

        r = c.post("/packs",
            json=[{
                "pack_id": "p-enqueue-test",
                "campaign_id": 1,
                "recipient_hash": "rh",
                "content_hash": "ch",
                "send_at": "2026-05-28T12:00:00Z",
                "key_id": "K_temp_x",
                "encrypted_blob": base64.b64encode(b"blob").decode(),
            }],
            headers={"Authorization": f"Bearer {bearer}"},
        )

    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    assert len(sent_messages) == 1
    assert sent_messages[0]["pack_id"] == "p-enqueue-test"
