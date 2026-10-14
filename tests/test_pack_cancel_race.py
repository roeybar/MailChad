"""A cancel can only win while a pack is pending."""
import json
import os
import random
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def cloud(tmp_cloud_dynamo, monkeypatch):
    from mailman.cloud import dynamo, keys_dynamo, packs, sync

    monkeypatch.setattr(packs, "SQS_QUEUE_URL", "")
    app = FastAPI()
    app.include_router(packs.router)
    app.dependency_overrides[sync.require_bearer] = lambda: "operator"
    return SimpleNamespace(c=TestClient(app, raise_server_exceptions=False),
                           dynamo=dynamo, keys_dynamo=keys_dynamo)


def _put(pid, campaign_id=7, payload=b"\x01"):
    from mailman.cloud import dynamo

    dynamo.put_pack(
        pack_id=pid, campaign_id=campaign_id,
        recipient_hash="r", content_hash="c",
        send_at="2026-01-01T00:00:00Z", key_id="k",
        encrypted_payload=payload, pushed_by="operator",
    )


def _push_real(pid):
    from mailman.cloud import dynamo, keys_dynamo
    from mailman.cloud.encryption_cloud import encrypt_with_temp

    payload = b'{"from":"a@b.com","recipient":"c@d.com","subject":"hi","html":"<p>hi</p>","resend_api_key":"re_test"}'
    k = os.urandom(32)
    blob = encrypt_with_temp(payload, k)
    keys_dynamo.set_k_temp(k, 3600, set_by="operator")
    dynamo.put_pack(
        pack_id=pid, campaign_id=1,
        recipient_hash="h1", content_hash="c1",
        send_at="2026-05-28T12:00:00Z",
        key_id="K_temp_test", encrypted_payload=blob,
        pushed_by="operator",
    )


def _finalize_sent(pid):
    from mailman.cloud import dynamo

    dynamo.claim_pack(pid)
    dynamo.update_pack(pid, status="sent", resend_message_id="re-1",
                       encrypted_payload=None)


def _record(pid):
    return {"body": json.dumps({"pack_id": pid}), "messageId": "msg-001",
            "receiptHandle": "rh-001", "attributes": {}}


class _LaggingGSI:
    def __init__(self, real, hook):
        self.real = real
        self.hook = hook
        self.called = False

    def query(self, **kw):
        response = self.real.query(**kw)
        if not self.called and kw.get("IndexName") == "status-index":
            self.called = True
            self.hook()
        return response

    def __getattr__(self, name):
        return getattr(self.real, name)


class _ClaimBeforeWrite:
    def __init__(self, real, hook):
        self.real = real
        self.hook = hook
        self.called = False

    def update_item(self, **kw):
        if not self.called:
            self.called = True
            self.hook()
        return self.real.update_item(**kw)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.parametrize("state", ["claimed", "sent", "failed", "stuck_no_key",
                                   "cancelled", "missing"])
def test_cancel_refused_once_not_pending(cloud, state):
    pid = "refused"
    if state == "missing":
        response = cloud.c.delete(f"/packs/{pid}")
        assert response.status_code == 404
        assert cloud.dynamo.get_pack(pid) is None
        return

    _put(pid)
    if state == "claimed":
        cloud.dynamo.claim_pack(pid)
    elif state == "sent":
        _finalize_sent(pid)
    elif state in ("failed", "stuck_no_key"):
        cloud.dynamo.claim_pack(pid)
        cloud.dynamo.update_pack(pid, status=state)
    else:
        assert cloud.c.delete(f"/packs/{pid}").json() == {"cancelled": True}

    response = cloud.c.delete(f"/packs/{pid}")
    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "reason": f"already {state}"}
    pack = cloud.dynamo.get_pack(pid)
    assert pack["status"] == state
    if state == "claimed":
        assert pack["encrypted_payload"] == b"\x01"


@pytest.mark.parametrize("hook_point", ["after_claim", "at_send", "after_finalize"])
def test_dispatcher_claim_races_cancel(cloud, monkeypatch, hook_point):
    from mailman.cloud import dispatcher_lambda

    pid = "dispatch-race"
    _push_real(pid)
    responses = []

    def cancel():
        responses.append(cloud.c.delete(f"/packs/{pid}"))

    if hook_point == "after_claim":
        real_claim = cloud.dynamo.claim_pack

        def claim_then_cancel(pack_id):
            claimed = real_claim(pack_id)
            cancel()
            return claimed

        monkeypatch.setattr(cloud.dynamo, "claim_pack", claim_then_cancel)
    elif hook_point == "after_finalize":
        def stats_then_cancel(campaign_id, bounced):
            cancel()

        monkeypatch.setattr(dispatcher_lambda, "_update_campaign_stats", stats_then_cancel)

    fake_resend = MagicMock()
    fake_resend.status_code = 200
    fake_resend.json.return_value = {"id": "re-1"}

    def send(*args, **kw):
        if hook_point == "at_send":
            assert cloud.dynamo.get_pack(pid)["status"] == "claimed"
            cancel()
        return fake_resend

    with patch("httpx.post", side_effect=send):
        dispatcher_lambda.handler({"Records": [_record(pid)]}, None)

    assert len(responses) == 1
    assert responses[0].status_code == 200
    state = "sent" if hook_point == "after_finalize" else "claimed"
    assert responses[0].json() == {"cancelled": False, "reason": f"already {state}"}
    pack = cloud.dynamo.get_pack(pid)
    assert pack["status"] == "sent"
    assert pack["resend_message_id"] == "re-1"
    resolved = {row["pack_id"]: row for row in cloud.dynamo.list_resolved_packs()}
    assert resolved[pid]["status"] == "sent"


@pytest.mark.parametrize("hook_state", ["claim", "sent"])
def test_claim_lands_inside_cancel(cloud, monkeypatch, hook_state):
    from mailman.cloud import dispatcher_lambda

    pid = "claim-inside-cancel"
    if hook_state == "claim":
        _put(pid)

        def hook():
            cloud.dynamo.claim_pack(pid)
    else:
        _push_real(pid)

        def hook():
            dispatcher_lambda.handler({"Records": [_record(pid)]}, None)

    proxy = _ClaimBeforeWrite(cloud.dynamo._get_table(), hook)
    fake_resend = MagicMock()
    fake_resend.status_code = 200
    fake_resend.json.return_value = {"id": "re-1"}
    with patch("httpx.post", return_value=fake_resend):
        with monkeypatch.context() as cancel_patch:
            cancel_patch.setattr(cloud.dynamo, "_get_table", lambda: proxy)
            response = cloud.c.delete(f"/packs/{pid}")

    state = "claimed" if hook_state == "claim" else "sent"
    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "reason": f"already {state}"}
    pack = cloud.dynamo.get_pack(pid)
    assert pack["status"] == state
    if hook_state == "claim":
        assert pack["encrypted_payload"] == b"\x01"
    else:
        assert pack["resend_message_id"] == "re-1"


def _assert_campaign_mix(cloud):
    for pid in ("A1", "A2"):
        pack = cloud.dynamo.get_pack(pid)
        assert pack["status"] == "cancelled"
        assert pack["encrypted_payload"] is None
    pack = cloud.dynamo.get_pack("B")
    assert pack["status"] == "claimed"
    assert pack["encrypted_payload"] == b"\x01"
    assert cloud.dynamo.get_pack("C")["status"] == "sent"
    assert cloud.dynamo.get_pack("D")["status"] == "pending"


def test_cancel_campaign_mix(cloud):
    for pid in ("A1", "A2", "B", "C"):
        _put(pid)
    _put("D", campaign_id=8)
    cloud.dynamo.claim_pack("B")
    _finalize_sent("C")

    response = cloud.c.post("/packs/cancel-campaign", json={"campaign_id": 7})
    assert response.status_code == 200
    assert response.json() == {"cancelled": 2, "campaign_id": 7}
    _assert_campaign_mix(cloud)


def test_cancel_campaign_stale_gsi(cloud, monkeypatch):
    for pid in ("A1", "A2", "B", "C"):
        _put(pid)
    _put("D", campaign_id=8)

    def hook():
        cloud.dynamo.claim_pack("B")
        _finalize_sent("C")

    proxy = _LaggingGSI(cloud.dynamo._get_table(), hook)
    monkeypatch.setattr(cloud.dynamo, "_get_table", lambda: proxy)
    response = cloud.c.post("/packs/cancel-campaign", json={"campaign_id": 7})
    assert response.status_code == 200
    assert response.json() == {"cancelled": 2, "campaign_id": 7}
    _assert_campaign_mix(cloud)


def test_cancel_pending_then_dispatch_skips(cloud):
    from mailman.cloud import dispatcher_lambda

    pid = "cancel-first"
    _push_real(pid)
    response = cloud.c.delete(f"/packs/{pid}")
    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    pack = cloud.dynamo.get_pack(pid)
    assert pack["status"] == "cancelled"
    assert pack["encrypted_payload"] is None
    resolved = {row["pack_id"]: row for row in cloud.dynamo.list_resolved_packs()}
    assert resolved[pid]["status"] == "cancelled"
    with patch("httpx.post") as mock_post:
        result = dispatcher_lambda.handler({"Records": [_record(pid)]}, None)
    assert result["skipped"] == 1
    mock_post.assert_not_called()


def test_stress_interleavings(cloud, monkeypatch):
    rng = random.Random(5817)
    cancel_first = set()
    claimed = set()
    pids = [f"stress-{i}" for i in range(120)]
    for pid in pids:
        _put(pid, campaign_id=9)
        order = rng.choice(["cancel", "claim", "sent"])
        if order == "cancel":
            assert cloud.c.delete(f"/packs/{pid}").json() == {"cancelled": True}
            assert cloud.dynamo.claim_pack(pid) is False
            cancel_first.add(pid)
        elif order == "claim":
            assert cloud.dynamo.claim_pack(pid) is True
            claimed.add(pid)
            assert cloud.c.delete(f"/packs/{pid}").json() == {
                "cancelled": False, "reason": "already claimed"}
        else:
            _finalize_sent(pid)
            claimed.add(pid)
            assert cloud.c.delete(f"/packs/{pid}").json() == {
                "cancelled": False, "reason": "already sent"}

    response = cloud.c.post("/packs/cancel-campaign", json={"campaign_id": 9})
    assert response.json() == {"cancelled": 0, "campaign_id": 9}
    rows = [cloud.dynamo.get_pack(pid) for pid in pids]
    assert sum(row["status"] == "cancelled" for row in rows) == len(cancel_first)
    for row in rows:
        if row["resend_message_id"] is not None or row["pack_id"] in claimed:
            assert row["status"] != "cancelled"

    rng = random.Random(5817)
    pids = [f"lag-stress-{i}" for i in range(90)]
    for pid in pids:
        _put(pid, campaign_id=10)
    claimed = set(rng.sample(pids, 30))

    def hook():
        for pid in claimed:
            assert cloud.dynamo.claim_pack(pid) is True

    proxy = _LaggingGSI(cloud.dynamo._get_table(), hook)
    monkeypatch.setattr(cloud.dynamo, "_get_table", lambda: proxy)
    response = cloud.c.post("/packs/cancel-campaign", json={"campaign_id": 10})
    assert response.json() == {"cancelled": 60, "campaign_id": 10}
    for pid in claimed:
        assert cloud.dynamo.get_pack(pid)["status"] == "claimed"


def test_cancel_retries_when_pack_lands_pending(cloud, monkeypatch):
    pid = "lands-pending"
    real_cancel = cloud.dynamo.cancel_pack
    calls = []

    def cancel(pack_id):
        calls.append(pack_id)
        if len(calls) == 1:
            refused = real_cancel(pack_id)
            _put(pack_id)
            return refused
        return real_cancel(pack_id)

    monkeypatch.setattr(cloud.dynamo, "cancel_pack", cancel)
    response = cloud.c.delete(f"/packs/{pid}")
    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    assert cloud.dynamo.get_pack(pid)["status"] == "cancelled"


def test_cancel_not_confirmed_when_still_pending(cloud, monkeypatch):
    pid = "still-pending"
    _put(pid)
    cancel = MagicMock(return_value=False)
    monkeypatch.setattr(cloud.dynamo, "cancel_pack", cancel)

    response = cloud.c.delete(f"/packs/{pid}")
    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "reason": "cancel not confirmed"}
    assert cloud.dynamo.get_pack(pid)["status"] == "pending"
    assert cancel.call_count == 2
