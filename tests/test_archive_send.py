"""Closed campaigns refuse sends, cancel held mail, and explain cancelled stages."""
import ast
import json
from pathlib import Path

import pytest


@pytest.fixture
def rig(tmp_cloud_dynamo, tmp_path, monkeypatch):
    import asyncio
    import base64
    import hashlib
    import random
    import sqlite3
    import uuid
    from collections import Counter
    from types import SimpleNamespace

    import httpx
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import IntegrityError
    from mailman.cloud import dynamo, packs, sync
    from mailman.terminal import auth, db, encryption, launch, main, routes_admin, settings, sync_client

    monkeypatch.setattr(packs, "SQS_QUEUE_URL", "")
    cloud = FastAPI()
    cloud.include_router(packs.router)
    cloud.dependency_overrides[sync.require_bearer] = lambda: "operator"

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.sqlite")
    keys = tmp_path / "keys"; keys.mkdir()
    monkeypatch.setattr(encryption, "KEYS_DIR", keys)
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    db.init()
    # These form handlers redirect to HTML in main.app; the rig exercises the JSON API.
    terminal = FastAPI()
    controls = APIRouter()
    controls.routes.extend(r for r in routes_admin.router.routes
                           if r.endpoint in (routes_admin.pause_campaign,
                                             routes_admin.resume_campaign, routes_admin.retry_failed))
    terminal.include_router(controls)
    terminal.include_router(main.app.router)
    terminal.dependency_overrides[auth.require_session] = lambda: {"sub": "retry@example.com", "role": "admin"}

    class _RigClient(TestClient):
        def request(self, *args, **kw):
            response = super().request(*args, **kw)
            decode = response.json
            def _json(**kwargs):
                try:
                    return decode(**kwargs)
                except ValueError as exc:
                    raise AssertionError(f"non-JSON response: {response.request.method} {response.request.url} "
                                         f"status={response.status_code} body={response.text!r}") from exc
            response.json = _json
            return response

    c = _RigClient(terminal, raise_server_exceptions=False)
    cloud_client = _RigClient(cloud, raise_server_exceptions=False)
    (keys / "cloud_bearer.txt").write_text("tok")
    settings.set("email_footer_address", "1 Example Street")

    def _post(c, path, body):
        r = c.post(path, json=body)
        assert r.status_code in (200, 201), (path, r.status_code, r.text[:300])
        return r.json()

    eid = _post(c, "/admin/entities", {"name": "Demo Co", "domain": "demo.example", "from_name": "Demo Co",
                "from_email": "hello@demo.example", "footer_address": "1 Example Street",
                "support_email": "support@demo.example", "public_host": "localhost"})["id"]

    # Key provisioning posts to /key/temp and writes key files; it is not under test.
    k = encryption.mint_k_temp()
    async def _fixed_k_temp(bearer):
        return k
    monkeypatch.setattr(launch, "_ensure_k_temp", _fixed_k_temp)
    # Vault decryption is not under test.
    def _credentials(eid):
        return {"resend_key": "re_x1", "from_name": "Demo Co", "from_email": "hello@demo.example", "domain": "demo.example"}
    monkeypatch.setattr(routes_admin, "get_entity_credentials", _credentials)

    class _CloudClient(httpx.AsyncClient):
        def __init__(self, *args, **kw):
            kw["transport"] = httpx.ASGITransport(app=cloud, raise_app_exceptions=False)
            super().__init__(*args, **kw)

    monkeypatch.setattr(launch.httpx, "AsyncClient", _CloudClient)
    monkeypatch.setattr(launch, "CLOUD_URL", "http://cloud.test")
    monkeypatch.setattr(launch, "PACK_PUSH_CHUNK", 2)

    def _campaign(c, n, tag):
        for i in range(n):
            _post(c, "/admin/contacts", {"email": f"{tag}{i}@demo.example", "name": f"Contact {i}",
                  "tags": tag, "consent_source": "demo"})
        tid = _post(c, "/admin/templates", {"name": f"T {tag}", "subject": "Hello", "from_name": "Demo Co",
                    "html_body": "<p>Hello {{name}}</p>", "text_body": "Hello"})["id"]
        cid = _post(c, "/admin/campaigns", {"name": f"C {tag}", "template_id": tid, "entity_id": eid})["id"]
        _post(c, f"/admin/campaigns/{cid}/recipients", {"by_tag": tag})
        routes_admin.mark_tested(cid)
        return cid

    def _cloud_packs(cid):
        rows, kw = [], {}
        while True:
            page = dynamo._get_table().scan(**kw)
            rows.extend(p for p in page["Items"] if p["pk"].startswith("PACK#") and p["campaign_id"] == cid)
            if "LastEvaluatedKey" not in page:
                return rows
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _sql(q, args=()):
        con = sqlite3.connect(str(tmp_path / "state.sqlite"))
        try:
            rows = con.execute(q, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    put_pack, record_dispatch = dynamo.put_pack, launch._record_dispatch
    faults_fired = 0
    def _fault_put(at):
        calls = 0
        def _put(**kw):
            nonlocal calls, faults_fired
            calls += 1
            if calls == at:
                faults_fired += 1
                raise RuntimeError("injected")
            return put_pack(**kw)
        monkeypatch.setattr(dynamo, "put_pack", _put)

    def _fail_record_once():
        # A real DB lock waits for the 10 s busy timeout and is nondeterministic.
        calls = 0
        def _record(*args, **kw):
            nonlocal calls, faults_fired
            calls += 1
            if calls == 1:
                faults_fired += 1
                raise sqlite3.OperationalError("database is locked (injected)")
            return record_dispatch(*args, **kw)
        monkeypatch.setattr(launch, "_record_dispatch", _record)

    def _clear_faults():
        nonlocal faults_fired
        faults_fired = 0
        monkeypatch.setattr(dynamo, "put_pack", put_pack)
        monkeypatch.setattr(launch, "_record_dispatch", record_dispatch)

    def _one_each(cid):
        held = _cloud_packs(cid)
        statuses = _sql("SELECT status FROM campaign_recipients WHERE campaign_id=?", (cid,))
        assert len(held) == len(statuses)
        assert len({p["recipient_hash"] for p in held}) == len(held)
        assert {p["pack_id"] for p in held} == {j[0] for j in _sql(
            "SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}
        assert all(s[0] == "sent" for s in statuses)

    def _approve(cid):
        r = c.post(f"/admin/campaigns/{cid}/batches/1/approve")
        assert r.status_code == 200, r.text[:300]
        return r.json()

    stage_template_no = 0
    def _stage(cid, number):
        nonlocal stage_template_no
        stage_template_no += 1
        tid = _post(c, "/admin/templates", {"name": f"Stage {cid} {number} {stage_template_no}", "subject": f"Stage {number}",
                    "from_name": "Demo Co", "html_body": f"<p>Stage {number}</p>", "text_body": "Hello"})["id"]
        _sql("INSERT INTO campaign_stages(campaign_id, stage_number, template_id, scheduled_for) "
             "VALUES (?, ?, ?, '2026-01-01 00:00:00')", (cid, number, tid))
        sid = _sql("SELECT id FROM campaign_stages WHERE campaign_id=? AND stage_number=?", (cid, number))[0][0]
        return tid, sid

    def _fail_pack(pid):
        dynamo.update_pack(pid, status="failed")
        sync_client._apply_pack_outcome({"pack_id": pid, "status": "failed"})

    def _gen_of(cid):
        created, gen = _sql("SELECT created_at, relaunch_gen FROM campaigns WHERE id=?", (cid,))[0]
        contacts = [r[0] for r in _sql(
            "SELECT contact_id FROM campaign_recipients WHERE campaign_id=? ORDER BY contact_id", (cid,))]
        keys_ = ["main"] + [f"relaunch:{g}" for g in range(1, gen + 3)]
        keys_ += [f"stage:{r[0]}" for r in _sql("SELECT id FROM campaign_stages WHERE campaign_id=?", (cid,))]
        lookup = {launch._pack_id(cid, created, key, contact, attempt):
                  SimpleNamespace(gen_key=key, contact_id=contact, attempt=attempt)
                  for key in keys_ for contact in contacts for attempt in range(6)}
        result = {}
        for p in _cloud_packs(cid):
            pid = p["pack_id"]
            assert pid in lookup, ("stray pack id", cid, pid)
            result[pid] = lookup[pid]
        return result

    def _per_gen_live(cid):
        mapped = _gen_of(cid)
        return Counter((mapped[p["pack_id"]].gen_key, mapped[p["pack_id"]].contact_id)
                       for p in _cloud_packs(cid)
                       if p["status"] not in ("failed", "cancelled", "stuck_no_key"))

    yield SimpleNamespace(c=c, cloud=cloud_client, launch=launch, dynamo=dynamo, sql=_sql,
                          campaign=_campaign, cloud_packs=_cloud_packs, fault_put=_fault_put,
                          fail_record_once=_fail_record_once, clear_faults=_clear_faults,
                          faults_fired=lambda: faults_fired,
                          one_each=_one_each, approve=_approve, stage=_stage, asyncio=asyncio,
                          random=random, uuid=uuid, hashlib=hashlib, base64=base64,
                          IntegrityError=IntegrityError, monkeypatch=monkeypatch,
                          fail_pack=_fail_pack, gen_of=_gen_of, per_gen_live=_per_gen_live,
                          db=db, db_path=tmp_path / "state.sqlite", keys=keys, settings=settings)
    c.close()
    cloud_client.close()
    terminal.dependency_overrides.clear()
    cloud.dependency_overrides.clear()


ARCHIVED = "campaign {id} is archived, and an archived campaign sends no mail. Clone it to send it again."
CANCELLED_REASON = "the campaign was cancelled before this stage went out"


def _arch(rig, cid):
    from mailman.terminal import routes_admin
    return rig.asyncio.run(routes_admin.archive_campaign(cid))


def _tick(rig):
    from mailman.terminal import sync_client
    return rig.asyncio.run(sync_client.scheduler_tick())


def _ids(rig, cid):
    return {p["pack_id"] for p in rig.cloud_packs(cid)}


def _audit(rig, cid, action):
    return [json.loads(r[0]) for r in rig.sql(
        "SELECT details_json FROM audit_event WHERE target=? AND action=? ORDER BY id",
        (f"campaign:{cid}", action))]


def _status(rig, cid):
    return rig.sql("SELECT status FROM campaigns WHERE id=?", (cid,))[0][0]


def _stage_status(rig, sid):
    return rig.sql("SELECT status FROM campaign_stages WHERE id=?", (sid,))[0][0]


def _batched(rig, tag):
    rig.settings.set("send_batch_size", "2")
    cid = rig.campaign(rig.c, 5, tag)
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 200, r.text
    rig.sql("UPDATE campaign_batches SET status='drained', approve_unlock_at=NULL "
            "WHERE campaign_id=? AND batch_no=1", (cid,))
    return cid


@pytest.mark.parametrize("body", [True, False], ids=["body", "no_body"])
def test_relaunch_refused_for_archived(rig, body):
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "arch-relaunch")
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 200, r.text
    rig.monkeypatch.setenv("CLOUD_URL", "http://cloud.test")
    _arch(rig, cid)
    before = _ids(rig, cid)
    state = rig.sql("SELECT relaunch_seq, relaunch_gen, relaunch_open FROM campaigns WHERE id=?", (cid,))
    launched = _audit(rig, cid, "campaign.launched")
    url = f"/admin/campaigns/{cid}/relaunch"
    r = rig.c.post(url, json={"expected_seq": state[0][0]}) if body else rig.c.post(url)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == ARCHIVED.format(id=cid)
    assert _ids(rig, cid) == before
    assert rig.sql("SELECT relaunch_seq, relaunch_gen, relaunch_open FROM campaigns WHERE id=?", (cid,)) == state
    assert _audit(rig, cid, "campaign.launched") == launched


def test_relaunch_finish_refused_for_archived(rig):
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "arch-finish")
    rig.fault_put(4)
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 400, r.text
    marker = rig.sql("SELECT dispatch_started_at FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,))
    assert marker[0][0] is not None
    rig.monkeypatch.delenv("CLOUD_URL", raising=False)
    _arch(rig, cid)
    assert _audit(rig, cid, "campaign.cancelled")[0]["markers_cleared"] is False
    before = _ids(rig, cid)
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == ARCHIVED.format(id=cid)
    assert _ids(rig, cid) == before
    assert rig.sql("SELECT dispatch_started_at FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == marker


@pytest.mark.parametrize("archive", ["legacy", "real_archive"])
def test_approve_refused_for_archived(rig, archive):
    assert rig.launch._active_seed_emails() == []
    cid = _batched(rig, "arch-approve")
    if archive == "legacy":
        rig.sql("UPDATE campaigns SET status='archived' WHERE id=?", (cid,))
    else:
        rig.monkeypatch.setenv("CLOUD_URL", "http://cloud.test")
        _arch(rig, cid)
    before = _ids(rig, cid)
    r = rig.c.post(f"/admin/campaigns/{cid}/batches/2/approve")
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == (ARCHIVED.format(id=cid) if archive == "legacy"
                                   else "batch 2 is 'cancelled', not pending")
    assert _ids(rig, cid) == before
    if archive == "legacy":
        assert rig.sql("SELECT status, approved_at, dispatch_started_at FROM campaign_batches "
                       "WHERE campaign_id=? AND batch_no=2", (cid,)) == [("pending", None, None)]
        assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("drained",)]


def test_approve_refused_for_cancelled(rig):
    assert rig.launch._active_seed_emails() == []
    cid = _batched(rig, "cancel-approve")
    rig.monkeypatch.setenv("CLOUD_URL", "http://cloud.test")
    r = rig.c.post(f"/admin/campaigns/{cid}/cancel")
    assert r.status_code == 200, r.text
    before = _ids(rig, cid)
    r = rig.c.post(f"/admin/campaigns/{cid}/batches/2/approve")
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "batch 2 is 'cancelled', not pending"
    assert _ids(rig, cid) == before


def test_relaunch_dispatched_control(rig):
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "relaunch-control")
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 200, r.text
    before = _ids(rig, cid)
    assert len(before) == 5
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "relaunch"
    assert len(_ids(rig, cid) - before) == 5
    other = _batched(rig, "approve-control")
    r = rig.c.post(f"/admin/campaigns/{other}/batches/2/approve")
    assert r.status_code == 200, r.text


def test_cancelled_campaign_stage_cancelled(rig):
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "cancel-stage")
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 200, r.text
    rig.monkeypatch.setenv("CLOUD_URL", "http://cloud.test")
    r = rig.c.post(f"/admin/campaigns/{cid}/cancel")
    assert r.status_code == 200, r.text
    tid, sid = rig.stage(cid, 1)
    before = _ids(rig, cid)
    _tick(rig)
    _tick(rig)
    assert _stage_status(rig, sid) == "cancelled"
    assert _audit(rig, cid, "campaign.stage_cancelled") == [{"stage_id": sid, "reason": CANCELLED_REASON}]
    assert _audit(rig, cid, "campaign.stage_failed") == []
    assert _audit(rig, cid, "campaign.stage_retrying") == []
    assert _ids(rig, cid) == before
    assert all(not p.gen_key.startswith("stage:") for p in rig.gen_of(cid).values())
    page = rig.c.get(f"/admin/campaigns/{cid}/recipients")
    assert page.status_code == 200, page.text
    assert "Not sent: " + CANCELLED_REASON + "." in page.text


@pytest.mark.parametrize("layer", ["gate", "still_due", "launch_guard"])
def test_cancelled_stage_layers(rig, layer):
    from mailman.terminal import sync_client
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "stage-layers")
    rig.sql("UPDATE campaigns SET status='cancelled' WHERE id=?", (cid,))
    tid, sid = rig.stage(cid, 1)
    if layer == "gate":
        rig.monkeypatch.setattr(sync_client, "_cancel_archived_stage", lambda *args: False)
        rig.monkeypatch.setattr(sync_client, "_stage_still_due", lambda *args: True)
        rig.monkeypatch.setattr(rig.launch, "_refuse_closed", lambda *args: None)
        _tick(rig)
    elif layer == "still_due":
        assert sync_client._stage_still_due(sid) is False
    else:
        with pytest.raises(rig.launch.CampaignClosed) as exc:
            rig.asyncio.run(rig.launch.launch_campaign(cid, stage_id=sid, template_id_override=tid))
        assert isinstance(exc.value, rig.launch.LaunchRetryable)
    assert _ids(rig, cid) == set()
    assert _stage_status(rig, sid) == "pending"


def test_cancel_stage_guard_direct(rig):
    from mailman.terminal import sync_client
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "stage-guard")
    tid, sid = rig.stage(cid, 1)
    rig.sql("UPDATE campaign_stages SET scheduled_for='2999-01-01 00:00:00' WHERE id=?", (sid,))
    assert sync_client._cancel_archived_stage(cid, sid, "cancelled") is False
    assert _audit(rig, cid, "campaign.stage_cancelled") == []
    rig.sql("UPDATE campaigns SET status='cancelled' WHERE id=?", (cid,))
    assert sync_client._cancel_archived_stage(cid, sid, "cancelled") is True
    assert _stage_status(rig, sid) == "cancelled"
    rows = [{"stage_id": sid, "reason": CANCELLED_REASON}]
    assert _audit(rig, cid, "campaign.stage_cancelled") == rows
    assert sync_client._cancel_archived_stage(cid, sid, "cancelled") is False
    with pytest.raises(KeyError):
        sync_client._cancel_archived_stage(cid, sid, "paused")
    assert _audit(rig, cid, "campaign.stage_cancelled") == rows


@pytest.mark.parametrize("work", ["cloud", "no_cloud", "drained"])
def test_archive_live_work(rig, work):
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "arch-live")
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 200, r.text
    assert len(rig.cloud_packs(cid)) == 5
    assert all(p["status"] == "pending" for p in rig.cloud_packs(cid))
    if work == "drained":
        rig.sql("UPDATE campaign_batches SET status='drained' WHERE campaign_id=?", (cid,))
        rig.sql("UPDATE campaigns SET relaunch_open=0 WHERE id=?", (cid,))
    if work == "no_cloud":
        rig.monkeypatch.delenv("CLOUD_URL", raising=False)
    else:
        rig.monkeypatch.setenv("CLOUD_URL", "http://cloud.test")
    assert _arch(rig, cid) == {"archived": True}
    assert _status(rig, cid) == "archived"
    assert len(_audit(rig, cid, "campaign.archived")) == 1
    cancelled = _audit(rig, cid, "campaign.cancelled")
    if work == "drained":
        assert cancelled == []
    else:
        assert len(cancelled) == 1
        assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("cancelled",)]
        if work == "cloud":
            assert all(p["status"] == "cancelled" for p in rig.cloud_packs(cid))
            assert cancelled[0]["packs_cancelled"] == 5
            assert cancelled[0]["markers_cleared"] is True
            assert rig.sql("SELECT dispatch_started_at FROM campaign_batches WHERE campaign_id=?", (cid,)) == [(None,)]
        else:
            assert all(p["status"] == "pending" for p in rig.cloud_packs(cid))
            assert cancelled[0]["markers_cleared"] is False


def test_ui_archive_route_awaits(rig):
    assert rig.launch._active_seed_emails() == []
    cid = rig.campaign(rig.c, 5, "arch-ui")
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 200, r.text
    rig.monkeypatch.setenv("CLOUD_URL", "http://cloud.test")
    r = rig.c.post(f"/admin/campaigns/{cid}/archive", follow_redirects=False)
    assert r.status_code == 200, r.text
    assert r.json() == {"archived": True}
    assert _status(rig, cid) == "archived"
    assert len(rig.cloud_packs(cid)) == 5
    assert all(p["status"] == "cancelled" for p in rig.cloud_packs(cid))


def test_refusal_precedes_every_plan_sends_caller(rig):
    assert rig.launch._active_seed_emails() == []
    tree = ast.parse(Path(rig.launch.__file__).read_text())
    callers = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        plans = [n.lineno for n in calls if n.func.id == "_plan_sends"]
        if plans:
            callers.append(node.name)
            refusals = [n.lineno for n in calls if n.func.id == "_refuse_closed"]
            assert refusals and min(refusals) < min(plans), node.name
    assert set(callers) == {"launch_campaign", "dispatch_batch"}


@pytest.mark.parametrize("seed", [5, 17, 29])
def test_walk_closed_campaigns(rig, seed):
    from fastapi import HTTPException
    assert rig.launch._active_seed_emails() == []
    rng = rig.random.Random(seed)
    rig.settings.set("send_batch_size", "2")
    campaigns = [rig.campaign(rig.c, 5, f"closed-walk-{i}") for i in range(3)]
    for cid in campaigns:
        r = rig.c.post(f"/admin/campaigns/{cid}/launch")
        assert r.status_code == 200, r.text
    history = []
    stale = set()
    for step in range(30):
        cid = rng.choice(campaigns)
        action = rng.choice(["launch", "approve", "relaunch", "stage", "archive", "cancel", "sql_archive"])
        status_before = {c: _status(rig, c) for c in campaigns}
        ids_before = {c: _ids(rig, c) for c in campaigns}
        history.append((step, cid, action, status_before[cid]))
        cloud_up = False
        r = None
        try:
            if action == "launch":
                r = rig.c.post(f"/admin/campaigns/{cid}/launch")
            elif action == "approve":
                rows = rig.sql("SELECT batch_no FROM campaign_batches WHERE campaign_id=? "
                               "AND status='pending' ORDER BY batch_no LIMIT 1", (cid,))
                if rows:
                    batch = rows[0][0]
                    rig.sql("UPDATE campaign_batches SET status='drained', approve_unlock_at=NULL "
                            "WHERE campaign_id=? AND batch_no=?", (cid, batch - 1))
                    r = rig.c.post(f"/admin/campaigns/{cid}/batches/{batch}/approve")
            elif action == "relaunch":
                r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
            elif action == "stage":
                number = rig.sql("SELECT coalesce(max(stage_number), 0) + 1 FROM campaign_stages "
                                 "WHERE campaign_id=?", (cid,))[0][0]
                rig.stage(cid, number)
                _tick(rig)
            elif action == "archive":
                cloud_up = rng.random() < 0.5
                with rig.monkeypatch.context() as m:
                    if cloud_up:
                        m.setenv("CLOUD_URL", "http://cloud.test")
                    else:
                        m.delenv("CLOUD_URL", raising=False)
                    _arch(rig, cid)
            elif action == "cancel":
                with rig.monkeypatch.context() as m:
                    m.setenv("CLOUD_URL", "http://cloud.test")
                    r = rig.c.post(f"/admin/campaigns/{cid}/cancel")
            else:
                rig.sql("UPDATE campaigns SET status='archived' WHERE id=?", (cid,))
        except Exception as exc:
            assert isinstance(exc, rig.launch.LaunchError) or (
                isinstance(exc, HTTPException) and exc.status_code == 400), (history, type(exc).__name__, str(exc))
        if r is not None:
            assert r.status_code in (200, 400), (history, r.status_code, r.text)
        for campaign in campaigns:
            added = _ids(rig, campaign) - ids_before[campaign]
            if status_before[campaign] == "archived":
                assert added == set(), history
            elif status_before[campaign] == "cancelled":
                mapped = rig.gen_of(campaign)
                assert all(not mapped[pid].gen_key.startswith("stage:") for pid in added), history
            if action == "stage" and _status(rig, campaign) in ("archived", "cancelled"):
                assert rig.sql("SELECT id FROM campaign_stages WHERE campaign_id=? AND status='pending'",
                               (campaign,)) == [], history
        if action == "archive" and not cloud_up or action == "sql_archive":
            stale.add(cid)   # mail already in the cloud was not cancelled, and a later Archive does not retry it
        if action == "archive" and cloud_up and cid not in stale:
            assert all(p["status"] != "pending" for p in rig.cloud_packs(cid)), history
