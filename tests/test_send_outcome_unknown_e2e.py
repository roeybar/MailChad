"""Cloud outcomes reach the terminal through the real polling and held-pack paths."""
import html
from unittest.mock import MagicMock, patch

import pytest

from test_pack_push_retry import rig  # noqa: F401
from test_send_outcome_unknown import _Resend, _rec, _tick


def _pull(rig, sync_client):
    rows = rig.dynamo.list_resolved_packs("", 500)
    target = rig.dynamo.outcome_cursor(max(rows, key=rig.dynamo._outcome_key))
    async def pull():
        stop = rig.asyncio.Event()
        task = rig.asyncio.create_task(sync_client.outcomes_loop(stop))
        async def reached_cursor():
            while sync_client._get_outcome_cursor() != target:
                await rig.asyncio.sleep(0)
        try:
            await rig.asyncio.wait_for(reached_cursor(), timeout=5)
        finally:
            stop.set()
            await rig.asyncio.wait_for(task, timeout=5)
    rig.asyncio.run(pull())


def _recipient(rig, cid, email):
    return rig.sql(
        "SELECT cr.status, cr.failure_reason, cr.message_id, j.status FROM campaign_recipients cr "
        "JOIN contacts c ON c.id=cr.contact_id "
        "JOIN dispatched_job j ON j.campaign_id=cr.campaign_id AND j.recipient_id=c.id "
        "WHERE cr.campaign_id=? AND c.email=?", (cid, email),
    )


def test_unknown_outcome_reaches_terminal_and_is_not_retried(rig, monkeypatch):
    from mailman.cloud import dispatcher_lambda, keys_dynamo
    from mailman.terminal import sync_client

    k = rig.asyncio.run(rig.launch._ensure_k_temp("tok"))
    keys_dynamo.set_k_temp(k, 3600, set_by="operator")
    _tick(monkeypatch)
    monkeypatch.setattr(rig.dynamo, "RESOLVED_SETTLE_S", 0)
    monkeypatch.setattr(dispatcher_lambda, "_enqueue", lambda pid: True)
    monkeypatch.setattr(dispatcher_lambda, "CLAIM_STALE_S", 900)
    dispatcher_lambda._campaign_stats.clear()
    monkeypatch.setattr(sync_client, "OUTCOMES_INTERVAL_S", 0)
    monkeypatch.setattr(sync_client, "CLOUD_BEARER", "tok")
    monkeypatch.setattr(sync_client, "CLOUD_URL", "http://cloud.test")
    cid = rig.campaign(rig.c, 3, "unk")
    response = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert response.status_code == 200, response.text
    by_hash = {p["recipient_hash"]: p["pack_id"] for p in rig.cloud_packs(cid)}
    ids = [by_hash[rig.hashlib.sha256(f"unk{i}@demo.example".encode()).hexdigest()] for i in range(3)]
    success = MagicMock()
    success.status_code = 200
    success.json.return_value = {"id": "re-ok"}
    resend = _Resend({"unk1@demo.example": 503, "unk2@demo.example": success})
    with patch("httpx.post", side_effect=resend):
        with patch.object(keys_dynamo, "get_active_k_temp", side_effect=RuntimeError("injected crash")):
            with pytest.raises(RuntimeError, match="injected crash"):
                dispatcher_lambda.handler({"Records": [_rec(ids[0])]}, None)
        assert rig.dynamo.get_pack(ids[0])["status"] == "claimed"
        with pytest.raises(dispatcher_lambda.SendOutcomeUnknown, match="resend 503"):
            dispatcher_lambda.handler({"Records": [_rec(ids[1])]}, None)
        assert dispatcher_lambda.handler({"Records": [_rec(ids[2])]}, None)["processed"] == 1
        _pull(rig, sync_client)
        assert _recipient(rig, cid, "unk1@demo.example") == [
            ("sent", "Delivery unknown: resend 503 during send", None, "dispatched"),
        ]
        assert _recipient(rig, cid, "unk0@demo.example") == [("sent", None, None, "dispatched")]
        rig.dynamo.update_pack(ids[0], claimed_at="2020-01-01T00:00:00Z")
        assert dispatcher_lambda.handler({"mode": "sweep"}, None)["stale_unknown"] == 1
        _pull(rig, sync_client)

    assert _recipient(rig, cid, "unk2@demo.example") == [("sent", None, "re-ok", "completed")]
    assert _recipient(rig, cid, "unk1@demo.example") == [
        ("sent", "Delivery unknown: resend 503 during send", None, "dispatched"),
    ]
    assert _recipient(rig, cid, "unk0@demo.example") == [
        ("sent", "Delivery unknown: dispatcher stopped after claiming it at 2020-01-01T00:00:00Z",
         None, "dispatched"),
    ]
    assert rig.sql("SELECT COUNT(*) FROM audit_event WHERE action='pack.outcome_unknown'") == [(2,)]
    page = rig.c.get(f"/admin/campaigns/{cid}/analytics")
    assert page.status_code == 200
    note = ("2 recipient(s) have an unknown delivery outcome: the mail provider failed while sending, "
            "so the mail may or may not have gone out. MailMan does not resend them, and Retry failed "
            "skips them. Check them in your Resend dashboard.")
    assert html.escape(note) in page.text
    assert page.text.count('>unknown</span>') == 2
    assert f'action="/admin/campaigns/{cid}/retry"' not in page.text
    assert "Retry 2 failed" not in page.text
    response = rig.c.post(f"/admin/campaigns/{cid}/retry")
    assert response.status_code == 200
    assert response.json() == {"reset": 0}
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 400
    assert len(rig.cloud_packs(cid)) == 3
    assert resend.calls == ["unk1@demo.example", "unk2@demo.example"]


def test_held_unknown_pack_gets_outcome(rig):
    cid = rig.campaign(rig.c, 5, "held")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    by_hash = {p["recipient_hash"]: p["pack_id"] for p in rig.cloud_packs(cid)}
    pid = by_hash[rig.hashlib.sha256(b"held0@demo.example").hexdigest()]
    rig.dynamo.update_pack(pid, status="unknown", failure_reason="resend 503 during send")
    rig.approve(cid)
    assert _recipient(rig, cid, "held0@demo.example") == [
        ("sent", "Delivery unknown: resend 503 during send", None, "dispatched"),
    ]
    assert len(rig.cloud_packs(cid)) == 5
