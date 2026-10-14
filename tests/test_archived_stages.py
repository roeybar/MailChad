"""Archived campaigns cancel pending stages once and show the reason."""
from __future__ import annotations
import asyncio, json, random, sqlite3
import pytest


REASON = "the campaign was archived before this stage went out"
NOTE = "Not sent: " + REASON + "."
PILL = '<span class="pill warn" style="flex-shrink:0"><span class="dot"></span>Stage 1</span>'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from mailman.terminal import auth, db, encryption, launch, main, routes_admin, settings
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.sqlite")
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setattr(encryption, "KEYS_DIR", keys)
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    db.init()
    main.app.dependency_overrides[auth.require_session] = lambda: {"sub": "scheduler@demo.example", "role": "admin"}
    c = TestClient(main.app)
    settings.set("email_footer_address", "1 Example Street")
    eid = _post(c, "/admin/entities", {
        "name": "Demo Co", "domain": "demo.example", "from_name": "Demo Co",
        "from_email": "hello@demo.example", "footer_address": "1 Example Street",
        "support_email": "support@demo.example", "public_host": "localhost"})["id"]
    (keys / "cloud_bearer.txt").write_text("tok")
    k = encryption.mint_k_temp()
    packs = []

    async def _k(bearer):
        return k

    def _creds(entity_id):
        return {"resend_key": "re_x1", "from_name": "Demo Co",
                "from_email": "hello@demo.example", "domain": "demo.example"}

    async def _push(p, bearer):
        packs.extend(p)
        n = len(p)
        return {"accepted": n, "submitted": n, "enqueued": 0, "deferred": 0}

    real = {"real_push": launch._push_packs, "real_creds": routes_admin.get_entity_credentials}
    monkeypatch.setattr(launch, "_ensure_k_temp", _k)
    monkeypatch.setattr(routes_admin, "get_entity_credentials", _creds)
    monkeypatch.setattr(launch, "_push_packs", _push)
    yield c, {"eid": eid, **real}, tmp_path, packs, k
    c.close()
    main.app.dependency_overrides.clear()


def _post(c, path, body):
    r = c.post(path, json=body)
    assert r.status_code in (200, 201), (path, r.status_code, r.text[:300])
    return r.json()


def _sql(tmp, q, args=()):
    con = sqlite3.connect(str(tmp / "state.sqlite"))
    try:
        rows = con.execute(q, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def tick(n=1):
    from mailman.terminal import sync_client
    for _ in range(n):
        asyncio.run(sync_client.scheduler_tick())


def _template(c, tag, body):
    return _post(c, "/admin/templates", {"name": f"T {tag}", "subject": "Hi", "from_name": "Demo Co",
                                        "html_body": body, "text_body": "x"})["id"]


def _mk(c, tag, html, n=2):
    from sqlalchemy import select
    from mailman.terminal import db, routes_admin
    from mailman.terminal.models import Entity
    for i in range(n):
        _post(c, "/admin/contacts", {"email": f"{tag}{i}@demo.example", "name": f"Person {i}",
                                    "tags": tag, "consent_source": "demo"})
    tid = _template(c, tag, html)
    with db.session() as s:
        eid = s.execute(select(Entity.id).where(Entity.name == "Demo Co")).scalar_one()
    cid = _post(c, "/admin/campaigns", {"name": f"C {tag}", "template_id": tid, "entity_id": eid})["id"]
    _post(c, f"/admin/campaigns/{cid}/recipients", {"by_tag": tag})
    routes_admin.mark_tested(cid)
    return cid


def _schedule(tmp, cid):
    # Past-due scheduled state, seeded directly: the schedule route refuses a time in the past.
    _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for='2026-01-01 09:00:00', "
              "updated_at=datetime('now','-1 minute') WHERE id=?", (cid,))


def _audit(tmp, cid, action):
    return [json.loads(r[0]) for r in _sql(
        tmp, "SELECT details_json FROM audit_event WHERE target=? AND action=? ORDER BY id",
        (f"campaign:{cid}", action))]


def _status(tmp, cid):
    return _sql(tmp, "SELECT status FROM campaigns WHERE id=?", (cid,))[0][0]


def _stage_status(tmp, sid):
    return _sql(tmp, "SELECT status FROM campaign_stages WHERE id=?", (sid,))[0][0]


def _page(c, path):
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


_STAGE_SEQ = [0]


def _add_stage(c, cid, when="2026-01-01 09:00:00", body="<p>Stage</p>"):
    _STAGE_SEQ[0] += 1   # template names are unique; a campaign may get several stages
    tid = _template(c, f"stage-{cid}-{_STAGE_SEQ[0]}", body)
    return _post(c, f"/admin/campaigns/{cid}/stages",
                 {"template_id": tid, "scheduled_for": when})["id"]


def _bad_stage(tmp, cid, tid, value):
    n = _sql(tmp, "SELECT coalesce(max(stage_number), 0) + 1 FROM campaign_stages "
                  "WHERE campaign_id=?", (cid,))[0][0]
    _sql(tmp, "INSERT INTO campaign_stages(campaign_id, stage_number, template_id, scheduled_for) "
              "VALUES (?, ?, ?, ?)", (cid, n, tid, value))
    return _sql(tmp, "SELECT id FROM campaign_stages WHERE campaign_id=? AND stage_number=?", (cid, n))[0][0]


def _archive(c, tmp, cid):
    r = c.post(f"/admin/campaigns/{cid}/archive", follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.json() == {"archived": True}
    assert _status(tmp, cid) == "archived"


def _count(tmp, action, cid):
    return _sql(tmp, "SELECT COUNT(*) FROM audit_event WHERE target=? AND action=?",
                (f"campaign:{cid}", action))[0][0]


def _recorder(monkeypatch, tmp):
    from mailman.terminal import launch
    real = launch.launch_campaign
    calls = []

    async def _record(campaign_id, **kw):
        calls.append((campaign_id, kw.get("stage_id"), _status(tmp, campaign_id)))
        return await real(campaign_id, **kw)

    monkeypatch.setattr(launch, "launch_campaign", _record)
    return calls


def test_archived_failed_campaign_stage_never_fires(rig, monkeypatch):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "arch-failed", "<p>{{previewText}}</p>")
    _schedule(tmp, cid)
    tick()
    assert _status(tmp, cid) == "failed"
    sid = _add_stage(c, cid)
    _archive(c, tmp, cid)
    calls = _recorder(monkeypatch, tmp)
    tick(3)
    assert calls == []
    assert packs == []
    assert _stage_status(tmp, sid) == "cancelled"
    assert _audit(tmp, cid, "campaign.stage_cancelled") == [{"stage_id": sid, "reason": REASON}]
    assert _count(tmp, "campaign.stage_failed", cid) == 0
    assert _sql(tmp, "SELECT COUNT(*) FROM drift_report WHERE job_id=?", (f"stage:{sid}",)) == [(0,)]
    assert _count(tmp, "campaign.archived", cid) == 1
    page = _page(c, f"/admin/campaigns/{cid}/recipients")
    assert 'callout warn' in page
    assert NOTE in page
    assert PILL in page
    assert "may already be on its way" not in page
    assert "campaign.stage_cancelled" in _page(c, "/admin/audit")


@pytest.mark.parametrize("prior", ["dispatched", "cancelled", "tested", "scheduled", "paused", "draft", "sent"])
def test_archived_stage_never_fires_for_each_prior_status(rig, monkeypatch, prior):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, f"prior-{prior}", "<p>Hello</p>")
    sid = _add_stage(c, cid)
    _sql(tmp, "UPDATE campaigns SET status=? WHERE id=?", (prior, cid))
    if prior == "scheduled":
        _sql(tmp, "UPDATE campaigns SET scheduled_for='2026-01-01 09:00:00' WHERE id=?", (cid,))
    _archive(c, tmp, cid)
    calls = _recorder(monkeypatch, tmp)
    tick(2)
    assert calls == []
    assert packs == []
    assert _stage_status(tmp, sid) == "cancelled"
    assert _audit(tmp, cid, "campaign.stage_cancelled") == [{"stage_id": sid, "reason": REASON}]
    assert NOTE in _page(c, f"/admin/campaigns/{cid}/recipients")


def test_failed_campaign_not_archived_still_fires_stage(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "failed-control", "<p>{{previewText}}</p>")
    _schedule(tmp, cid)
    tick()
    assert _status(tmp, cid) == "failed"
    sid = _add_stage(c, cid)
    tick()
    assert _stage_status(tmp, sid) == "dispatched"
    assert len(packs) == 2
    assert _count(tmp, "campaign.stage_cancelled", cid) == 0


def test_selection_gate_holds_without_cancel_step(rig, monkeypatch):
    from mailman.terminal import sync_client
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "selection-gate", "<p>Hello</p>")
    sid = _add_stage(c, cid)
    _archive(c, tmp, cid)
    monkeypatch.setattr(sync_client, "_cancel_archived_stage", lambda *args: False)
    monkeypatch.setattr(sync_client, "_stage_still_due", lambda *args: True)
    calls = _recorder(monkeypatch, tmp)
    tick(2)
    assert calls == []
    assert packs == []
    assert _stage_status(tmp, sid) == "pending"
    assert _count(tmp, "campaign.stage_failed", cid) == 0


def test_archive_during_tick_stops_later_stages(rig, monkeypatch):
    from mailman.terminal import launch, routes_admin
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "mid-tick", "<p>Hello</p>")
    stages = {_add_stage(c, cid), _add_stage(c, cid)}
    real = launch.launch_campaign
    calls = []

    async def _archive_first(campaign_id, **kw):
        calls.append(kw["stage_id"])
        result = await real(campaign_id, **kw)
        if len(calls) == 1:
            await routes_admin.archive_campaign(cid)
        return result

    monkeypatch.setattr(launch, "launch_campaign", _archive_first)
    tick()
    assert len(calls) == 1
    sent = calls[0]
    pending = (stages - {sent}).pop()
    assert _stage_status(tmp, sent) == "dispatched"
    assert _stage_status(tmp, pending) == "pending"
    assert len(packs) == 2
    tick()
    assert calls == [sent]
    assert _stage_status(tmp, pending) == "cancelled"
    assert _audit(tmp, cid, "campaign.stage_cancelled") == [{"stage_id": pending, "reason": REASON}]
    assert len(packs) == 2


def test_cancel_archived_stage_guard(rig):
    from mailman.terminal import sync_client
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "cancel-guard", "<p>Hello</p>")
    sid = _add_stage(c, cid, "2999-01-01 00:00:00")
    assert sync_client._cancel_archived_stage(cid, sid) is False
    assert _count(tmp, "campaign.stage_cancelled", cid) == 0
    _archive(c, tmp, cid)
    _sql(tmp, "UPDATE campaign_stages SET status='dispatched' WHERE id=?", (sid,))
    assert sync_client._cancel_archived_stage(cid, sid) is False
    assert _count(tmp, "campaign.stage_cancelled", cid) == 0
    _sql(tmp, "UPDATE campaign_stages SET status='pending' WHERE id=?", (sid,))
    assert sync_client._cancel_archived_stage(cid, sid) is True
    assert _stage_status(tmp, sid) == "cancelled"
    assert _audit(tmp, cid, "campaign.stage_cancelled") == [{"stage_id": sid, "reason": REASON}]
    assert sync_client._cancel_archived_stage(cid, sid) is False
    assert _count(tmp, "campaign.stage_cancelled", cid) == 1


@pytest.mark.parametrize("layer", ["cancel_step", "gate_only"])
def test_unreadable_stage_of_archived_campaign(rig, monkeypatch, layer):
    from mailman.terminal import sync_client
    c, ids, tmp, packs, k = rig
    cid = _mk(c, f"unreadable-{layer}", "<p>Hello</p>")
    tid = _template(c, f"bad-time-{layer}", "<p>Stage</p>")
    sid = _bad_stage(tmp, cid, tid, "garbage")
    _archive(c, tmp, cid)
    if layer == "gate_only":
        monkeypatch.setattr(sync_client, "_cancel_archived_stage", lambda *args: False)
    tick(2)
    assert _stage_status(tmp, sid) == ("cancelled" if layer == "cancel_step" else "pending")
    assert _count(tmp, "campaign.stage_cancelled", cid) == (1 if layer == "cancel_step" else 0)
    if layer == "cancel_step":
        assert _audit(tmp, cid, "campaign.stage_cancelled") == [{"stage_id": sid, "reason": REASON}]
    assert _count(tmp, "campaign.stage_failed", cid) == 0
    assert _sql(tmp, "SELECT COUNT(*) FROM drift_report WHERE job_id=?", (f"stage:{sid}",)) == [(0,)]
    assert packs == []


def test_unarchive_by_mark_tested(rig):
    from mailman.terminal import routes_admin
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "unarchive-after", "<p>Hello</p>")
    sid = _add_stage(c, cid)
    _archive(c, tmp, cid)
    tick()
    assert _stage_status(tmp, sid) == "cancelled"
    routes_admin.mark_tested(cid)
    tick(2)
    assert _stage_status(tmp, sid) == "cancelled"
    assert packs == []
    assert _count(tmp, "campaign.stage_cancelled", cid) == 1
    new_sid = _add_stage(c, cid)
    tick()
    assert _stage_status(tmp, new_sid) == "dispatched"
    assert len(packs) == 2
    other = _mk(c, "unarchive-before", "<p>Hello</p>")
    other_sid = _add_stage(c, other)
    _archive(c, tmp, other)
    routes_admin.mark_tested(other)
    tick()
    assert _stage_status(tmp, other_sid) == "dispatched"
    assert _count(tmp, "campaign.stage_cancelled", other) == 0


def test_stage_added_to_archived_campaign_is_cancelled(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "added-after", "<p>Hello</p>")
    _archive(c, tmp, cid)
    past = _add_stage(c, cid)
    future = _add_stage(c, cid, "2999-01-01 00:00:00")
    tick()
    assert _stage_status(tmp, past) == _stage_status(tmp, future) == "cancelled"
    rows = _audit(tmp, cid, "campaign.stage_cancelled")
    assert sorted(rows, key=lambda r: r["stage_id"]) == [
        {"stage_id": sid, "reason": REASON} for sid in sorted([past, future])]
    assert packs == []


@pytest.mark.parametrize("seed", [11, 23, 37])
def test_walk_archive_stages(rig, monkeypatch, seed):
    from collections import Counter
    from mailman.terminal import routes_admin
    c, ids, tmp, packs, k = rig
    rng = random.Random(seed)
    campaigns = [_mk(c, f"walk-{seed}-{i}", "<p>Hello</p>", n=2) for i in range(5)]
    tids = {cid: _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
            for cid in campaigns}
    calls = _recorder(monkeypatch, tmp)
    for _ in range(40):
        cid = rng.choice(campaigns)
        action = rng.choices(["past", "future", "bad", "archive", "mark_tested", "tick"],
                             weights=[.25, .10, .10, .15, .10, .30])[0]
        if action == "past":
            _add_stage(c, cid)
        elif action == "future":
            _add_stage(c, cid, "2999-01-01 00:00:00")
        elif action == "bad":
            _bad_stage(tmp, cid, tids[cid], "garbage")
        elif action == "archive":
            _archive(c, tmp, cid)
        elif action == "mark_tested" and _status(tmp, cid) == "archived":
            routes_admin.mark_tested(cid)
        elif action == "tick":
            tick()
    tick(2)
    stage_calls = [(cid, sid, status) for cid, sid, status in calls if sid is not None]
    assert all(status != "archived" for cid, sid, status in stage_calls)
    launched = Counter(sid for cid, sid, status in stage_calls)
    for sid, cid, status in _sql(tmp, "SELECT id, campaign_id, status FROM campaign_stages"):
        if _status(tmp, cid) == "archived":
            assert status != "pending", (sid, cid)
        cancellations = [row for row in _audit(tmp, cid, "campaign.stage_cancelled") if row["stage_id"] == sid]
        failures = [row for row in _audit(tmp, cid, "campaign.stage_failed") if row["stage_id"] == sid]
        assert (status == "cancelled") == (len(cancellations) == 1), (sid, status, cancellations)
        if cancellations:
            assert cancellations == [{"stage_id": sid, "reason": REASON}]
            assert failures == []
        if status == "dispatched":
            assert launched[sid] == 1
    assert len(packs) == 2 * len(stage_calls)
