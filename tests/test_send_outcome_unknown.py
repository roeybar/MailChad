"""Unknown send outcomes resolve once and cannot cause an automatic resend."""
from __future__ import annotations

import json
import os
import random
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from boto3.dynamodb.conditions import Key
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("tmp_cloud_dynamo")


@pytest.fixture(autouse=True)
def _dispatcher(tmp_cloud_dynamo, monkeypatch):
    from mailman.cloud import dispatcher_lambda

    monkeypatch.setattr(dispatcher_lambda, "_enqueue", lambda pid: True)
    monkeypatch.setattr(dispatcher_lambda, "CLAIM_STALE_S", 900)
    dispatcher_lambda._campaign_stats.clear()


def _k():
    from mailman.cloud import keys_dynamo

    k = os.urandom(32)
    keys_dynamo.set_k_temp(k, 3600, set_by="operator")
    return k


def _push(pid, recipient, k):
    from mailman.cloud import dynamo
    from mailman.cloud.encryption_cloud import encrypt_with_temp

    payload = {"from": "hello@demo.example", "recipient": recipient,
               "subject": "Hello", "html": "<p>Hello</p>", "resend_api_key": "re_test"}
    dynamo.put_pack(
        pack_id=pid, campaign_id=1, recipient_hash="h-" + pid, content_hash="c-" + pid,
        send_at="2026-05-28T12:00:00Z", key_id="K_temp_test",
        encrypted_payload=encrypt_with_temp(json.dumps(payload).encode(), k),
        pushed_by="operator",
    )


def _rec(pid):
    return {"body": json.dumps({"pack_id": pid}), "messageId": "msg-" + pid,
            "receiptHandle": "rh-" + pid, "attributes": {}}


class _Resend:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.codes = []

    def __call__(self, url, *, json, headers, timeout):
        recipient = json["to"][0]
        self.calls.append(recipient)
        answer = self.answers(recipient) if callable(self.answers) else self.answers.get(recipient, 200)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, MagicMock):
            self.codes.append(answer.status_code)
            return answer
        self.codes.append(answer)
        response = MagicMock()
        response.status_code = answer
        response.json.return_value = {"id": "m1"} if answer == 200 else {}
        return response


def _cloud():
    from mailman.cloud import packs, sync

    app = FastAPI()
    app.include_router(packs.router)
    app.dependency_overrides[sync.require_bearer] = lambda: "operator"
    return TestClient(app)


def _tick(monkeypatch):
    from mailman.cloud import dynamo

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def clock():
        nonlocal now
        now += timedelta(seconds=1)
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(dynamo, "_now", clock)


def test_resend_5xx_marks_unknown_and_stops_batch():
    from mailman.cloud import dispatcher_lambda, dynamo

    k = _k()
    _push("p1", "first@demo.example", k)
    _push("p2", "second@demo.example", k)
    resend = _Resend({"first@demo.example": 503, "second@demo.example": 503})
    with patch("httpx.post", side_effect=resend):
        with pytest.raises(dispatcher_lambda.SendOutcomeUnknown, match="resend 503"):
            dispatcher_lambda.handler({"Records": [_rec("p1"), _rec("p2")]}, None)
    first = dynamo.get_pack("p1")
    assert first["status"] == "unknown"
    assert first["failure_reason"] == "resend 503 during send"
    assert first["encrypted_payload"] is None
    assert first["resolved_at"]
    assert dynamo.get_pack("p2")["status"] == "pending"
    assert resend.calls == ["first@demo.example"]


@pytest.mark.parametrize("error", [httpx.ConnectError("x"), httpx.ReadTimeout("x"),
                                   httpx.RemoteProtocolError("x")])
def test_network_error_marks_unknown(error):
    from mailman.cloud import dispatcher_lambda, dynamo

    _push("network", "network@demo.example", _k())
    resend = _Resend({"network@demo.example": error})
    reason = f"network error during send ({type(error).__name__})"
    with patch("httpx.post", side_effect=resend):
        with pytest.raises(dispatcher_lambda.SendOutcomeUnknown, match="network error during send"):
            dispatcher_lambda.handler({"Records": [_rec("network")]}, None)
    pack = dynamo.get_pack("network")
    assert pack["status"] == "unknown"
    assert pack["failure_reason"] == reason
    assert pack["encrypted_payload"] is None
    assert pack["resolved_at"]
    assert resend.calls == ["network@demo.example"]


def test_redelivery_after_unknown_never_resends():
    from mailman.cloud import dispatcher_lambda, dynamo

    k = _k()
    _push("p1", "first@demo.example", k)
    _push("p2", "second@demo.example", k)
    resend = _Resend({"first@demo.example": 503, "second@demo.example": 503})
    with patch("httpx.post", side_effect=resend):
        with pytest.raises(dispatcher_lambda.SendOutcomeUnknown):
            dispatcher_lambda.handler({"Records": [_rec("p1"), _rec("p2")]}, None)
        resend.answers = {}
        result = dispatcher_lambda.handler({"Records": [_rec("p1")]}, None)
    assert result["skipped"] == 1
    assert dynamo.get_pack("p1")["status"] == "unknown"
    assert dynamo.get_pack("p2")["status"] == "pending"
    assert resend.calls == ["first@demo.example"]


def test_crash_between_claim_and_send_then_sweep():
    from mailman.cloud import dispatcher_lambda, dynamo, keys_dynamo

    _push("crash", "crash@demo.example", _k())
    resend = _Resend()
    with patch("httpx.post", side_effect=resend):
        with patch.object(keys_dynamo, "get_active_k_temp", side_effect=RuntimeError("injected crash")):
            with pytest.raises(RuntimeError, match="injected crash"):
                dispatcher_lambda.handler({"Records": [_rec("crash")]}, None)
        assert dynamo.get_pack("crash")["status"] == "claimed"
        assert dispatcher_lambda.handler({"Records": [_rec("crash")]}, None)["skipped"] == 1
        assert dispatcher_lambda.handler({"mode": "sweep"}, None)["stale_unknown"] == 0
        assert dynamo.get_pack("crash")["status"] == "claimed"
        dynamo.update_pack("crash", claimed_at="2020-01-01T00:00:00Z")
        assert dispatcher_lambda.handler({"mode": "sweep"}, None)["stale_unknown"] == 1
        pack = dynamo.get_pack("crash")
        assert pack["status"] == "unknown"
        assert pack["failure_reason"] == "dispatcher stopped after claiming it at 2020-01-01T00:00:00Z"
        assert pack["encrypted_payload"] is None
        assert pack["resolved_at"]
        assert dispatcher_lambda.handler({"Records": [_rec("crash")]}, None)["skipped"] == 1
    assert resend.calls == []


def test_lost_write_after_2xx_resolves_unknown_without_resend():
    from mailman.cloud import dispatcher_lambda, dynamo

    _push("lost", "lost@demo.example", _k())
    update_pack = dynamo.update_pack
    def lost_write(pid, **fields):
        if fields.get("status") == "sent":
            raise RuntimeError("injected lost write")
        return update_pack(pid, **fields)
    resend = _Resend()
    with patch("httpx.post", side_effect=resend):
        with patch.object(dynamo, "update_pack", side_effect=lost_write):
            with pytest.raises(RuntimeError, match="injected lost write"):
                dispatcher_lambda.handler({"Records": [_rec("lost")]}, None)
        assert dynamo.get_pack("lost")["status"] == "claimed"
        assert resend.calls == ["lost@demo.example"]
        dynamo.update_pack("lost", claimed_at="2020-01-01T00:00:00Z")
        assert dispatcher_lambda.handler({"mode": "sweep"}, None)["stale_unknown"] == 1
        assert dynamo.get_pack("lost")["status"] == "unknown"
        assert dispatcher_lambda.handler({"Records": [_rec("lost")]}, None)["skipped"] == 1
    assert resend.calls == ["lost@demo.example"]


def test_stale_expire_is_conditional(monkeypatch):
    from mailman.cloud import dynamo

    _push("race", "race@demo.example", _k())
    assert dynamo.claim_pack("race") is True
    dynamo.update_pack("race", claimed_at="2020-01-01T00:00:00Z")
    table = dynamo._get_table()
    captured = table.query(IndexName="status-index", KeyConditionExpression=Key("status").eq("claimed"))
    assert [row["pack_id"] for row in captured["Items"]] == ["race"]
    dynamo.update_pack("race", status="sent")
    proxy = MagicMock(wraps=table)
    proxy.query.return_value = captured
    monkeypatch.setattr(dynamo, "_get_table", lambda: proxy)
    assert dynamo.expire_stale_claims(900) == 0
    assert dynamo.get_pack("race")["status"] == "sent"


@pytest.mark.parametrize("status", ["sent", "failed", "stuck_no_key", "unknown"])
def test_resolved_at_stamped_on_every_resolution(status):
    from mailman.cloud import dynamo

    k = _k()
    _push("resolved", "resolved@demo.example", k)
    dynamo.update_pack("resolved", status=status)
    assert dynamo.get_pack("resolved")["resolved_at"]
    _push("unresolved", "unresolved@demo.example", k)
    dynamo.update_pack("unresolved", status="claimed")
    assert dynamo.get_pack("unresolved")["resolved_at"] is None
    dynamo.update_pack("unresolved", failure_reason="still claimed")
    assert dynamo.get_pack("unresolved")["resolved_at"] is None
    _push("cancelled", "cancelled@demo.example", k)
    assert dynamo.cancel_pack("cancelled") is True
    assert dynamo.get_pack("cancelled")["resolved_at"]
    dynamo.update_pack("resolved", status=status, resolved_at="2020-01-01T00:00:00Z")
    assert dynamo.get_pack("resolved")["resolved_at"] == "2020-01-01T00:00:00Z"


def test_late_resolution_reaches_packs_status(monkeypatch):
    from mailman.cloud import dynamo

    _tick(monkeypatch)
    monkeypatch.setattr(dynamo, "RESOLVED_SETTLE_S", 0)
    k = _k()
    for pid in ("a", "b", "c"):
        _push(pid, pid + "@demo.example", k)
    assert dynamo.claim_pack("a") is True
    dynamo.update_pack("b", status="sent", encrypted_payload=None)
    dynamo.update_pack("c", status="sent", encrypted_payload=None)
    with _cloud() as cloud:
        response = cloud.get("/packs/status")
        assert response.status_code == 200
        first = response.json()
        assert [p["pack_id"] for p in first["packs"]] == ["b", "c"]
        assert first["max_id"] == dynamo.outcome_cursor(dynamo.get_pack("c"))
        dynamo.update_pack("a", claimed_at="2020-01-01T00:00:00Z")
        assert dynamo.expire_stale_claims(900) == 1
        second = cloud.get("/packs/status", params={"since_id": first["max_id"]}).json()
        assert [p["pack_id"] for p in second["packs"]] == ["a"]
        assert second["packs"][0]["status"] == "unknown"
        third = cloud.get("/packs/status", params={"since_id": second["max_id"]}).json()
        assert third["packs"] == []
        assert third["max_id"] == second["max_id"]


def test_packs_status_settles_newest_seconds(monkeypatch):
    from mailman.cloud import dynamo

    k = _k()
    _push("p", "p@demo.example", k)
    _push("q", "q@demo.example", k)
    dynamo.update_pack("p", status="sent", encrypted_payload=None)
    dynamo.update_pack("q", status="sent", encrypted_payload=None, resolved_at=dynamo._iso_ago(60))
    assert dynamo.RESOLVED_SETTLE_S == 10
    with _cloud() as cloud:
        settled = cloud.get("/packs/status").json()
        assert [p["pack_id"] for p in settled["packs"]] == ["q"]
        monkeypatch.setattr(dynamo, "RESOLVED_SETTLE_S", 0)
        all_rows = cloud.get("/packs/status").json()
        assert [p["pack_id"] for p in all_rows["packs"]] == ["q", "p"]


def test_ties_paged_exactly_once(monkeypatch):
    from mailman.cloud import dynamo

    monkeypatch.setattr(dynamo, "RESOLVED_SETTLE_S", 0)
    k = _k()
    for pid in ("e", "c", "a", "d", "b"):
        _push(pid, pid + "@demo.example", k)
        dynamo.update_pack(pid, status="sent", encrypted_payload=None, resolved_at="2026-01-01T00:00:00Z")
    cursor, found = "", []
    with _cloud() as cloud:
        for size in (2, 2, 1, 0):
            page = cloud.get("/packs/status", params={"since_id": cursor, "limit": 2}).json()
            assert len(page["packs"]) == size
            found.extend(p["pack_id"] for p in page["packs"])
            if page["packs"]:
                assert page["max_id"] == dynamo.outcome_cursor(page["packs"][-1])
            else:
                assert page["max_id"] == cursor
            cursor = page["max_id"]
    assert found == ["a", "b", "c", "d", "e"]
    assert len(found) == len(set(found))


def test_bare_cursor_keeps_strict_meaning():
    from mailman.cloud import dynamo

    times = ["2026-06-21T01:00:00Z", "2026-06-21T02:00:00Z", "2026-06-21T03:00:00Z"]
    for pid, enqueued_at in zip(("a", "b", "c"), times):
        dynamo._get_table().put_item(Item={
            "pk": f"PACK#{pid}", "sk": f"PACK#{pid}", "pack_id": pid, "campaign_id": 1,
            "recipient_hash": "h", "content_hash": "c", "send_at": "1970-01-01T00:00:00Z",
            "key_id": "K", "encrypted_payload": b"x", "status": "sent",
            "enqueued_at": enqueued_at, "pushed_by": "test", "resend_message_id": f"msg-{pid}",
        })
    assert [p["pack_id"] for p in dynamo.list_resolved_packs(times[1])] == ["c"]
    assert [p["pack_id"] for p in dynamo.list_resolved_packs(times[1] + "#a")] == ["b", "c"]


def test_stress_redelivery_never_double_sends():
    from mailman.cloud import dispatcher_lambda, dynamo, keys_dynamo

    rng = random.Random(1107)
    k = _k()
    ids = [f"stress-{i:02d}" for i in range(60)]
    for pid in ids:
        _push(pid, pid + "@demo.example", k)
    def answer(recipient):
        return rng.choices([200, 503, httpx.ReadTimeout("injected timeout")], weights=[50, 20, 15])[0]
    resend = _Resend(answer)
    queue = deque((pid, 1) for pid in ids)
    get_key = keys_dynamo.get_active_k_temp
    with patch("httpx.post", side_effect=resend):
        for _ in range(500):
            if queue:
                batch = [queue.popleft() for _ in range(min(10, len(queue)))]
                crash = rng.random() < 0.15
                def one_shot_key():
                    nonlocal crash
                    if crash:
                        crash = False
                        raise RuntimeError("injected crash")
                    return get_key()
                with patch.object(keys_dynamo, "get_active_k_temp", side_effect=one_shot_key):
                    try:
                        dispatcher_lambda.handler({"Records": [_rec(pid) for pid, receive in batch]}, None)
                    except RuntimeError as error:
                        assert isinstance(error, dispatcher_lambda.SendOutcomeUnknown) or str(error) == "injected crash"
                        queue.extend((pid, receive + 1) for pid, receive in batch if receive < 3)
            queue.extend((p["pack_id"], 1) for p in dynamo.list_pending_packs(limit=60))
            if rng.random() < 0.3:
                for pid in ids:
                    if dynamo.get_pack(pid)["status"] == "claimed":
                        dynamo.update_pack(pid, claimed_at="2020-01-01T00:00:00Z")
                dispatcher_lambda.handler({"mode": "sweep"}, None)
            rows = [dynamo.get_pack(pid) for pid in ids]
            if not queue and all(p["status"] not in ("pending", "claimed") for p in rows):
                break
        else:
            pytest.fail("send queue did not settle within 500 rounds")
        assert {p["status"] for p in rows} <= {"sent", "unknown"}
        assert max(Counter(resend.calls).values(), default=0) <= 1
        assert sum(p["status"] == "sent" for p in rows) == resend.codes.count(200)
        unknowns = [p for p in rows if p["status"] == "unknown"]
        assert unknowns
        for pack in unknowns:
            assert pack["failure_reason"] in (
                "resend 503 during send", "network error during send (ReadTimeout)",
                "dispatcher stopped after claiming it at 2020-01-01T00:00:00Z",
            )
        calls = list(resend.calls)
        for offset in range(0, len(ids), 10):
            result = dispatcher_lambda.handler({"Records": [_rec(pid) for pid in ids[offset:offset + 10]]}, None)
            assert result["skipped"] == 10
        assert resend.calls == calls
