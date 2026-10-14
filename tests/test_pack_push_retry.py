"""A partly failed pack push, then a retry, gives each recipient one pack.
dispatch_batch runs against the real cloud packs router on moto (tests/conftest.py tmp_cloud_dynamo).
"""
import re

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


def test_chunk2_rejected_records_chunk1_then_approve_next(rig):
    cid = rig.campaign(rig.c, 5, "a")
    rig.fault_put(3)
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("cloud /packs rejected (500) on chunk 2")
    assert len(rig.cloud_packs(cid)) == 2
    assert {p["pack_id"] for p in rig.cloud_packs(cid)} == {j[0] for j in rig.sql(
        "SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}
    assert rig.sql("SELECT status, COUNT(*) FROM campaign_recipients WHERE campaign_id=? GROUP BY status",
                   (cid,)) == [("queued", 3), ("sent", 2)]
    assert rig.sql("SELECT status FROM campaigns WHERE id=?", (cid,)) == [("dispatched",)]
    assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("pending",)]
    rig.approve(cid)
    rig.one_each(cid)
    assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("sending",)]


def test_chunk2_partial_store_then_approve_next(rig):
    cid = rig.campaign(rig.c, 5, "p")
    rig.fault_put(4)
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 400
    assert len(rig.cloud_packs(cid)) == 3
    assert len(rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))) == 2
    rig.approve(cid)
    rig.one_each(cid)


def test_record_failure_then_approve_next(rig):
    cid = rig.campaign(rig.c, 5, "b")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    assert len(rig.cloud_packs(cid)) == 5
    assert rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,)) == []
    assert rig.sql("SELECT status FROM campaign_recipients WHERE campaign_id=?", (cid,)) == [("queued",)] * 5
    ack = rig.approve(cid)["cloud_ack"]
    assert ack["accepted"] == 0 and ack["submitted"] == 5
    rig.one_each(cid)


def test_record_failure_then_relaunch(rig):
    cid = rig.campaign(rig.c, 5, "r")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200
    assert r.json()["mode"] == "finish-batch"
    rig.one_each(cid)


def test_relaunch_after_recorded_send_is_new_send(rig):
    cid = rig.campaign(rig.c, 5, "d")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 200
    held = rig.cloud_packs(cid)
    assert len(held) == 10
    assert len({p["recipient_hash"] for p in held}) == 5
    assert all(sum(p["recipient_hash"] == h for p in held) == 2 for h in {p["recipient_hash"] for p in held})
    assert len(rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))) == 10
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 200
    assert len(rig.cloud_packs(cid)) == 15
    assert rig.sql("SELECT relaunch_gen, relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(2, 0)]


def test_held_resolved_packs_get_their_outcome(rig):
    cid = rig.campaign(rig.c, 5, "f")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    by_hash = {p["recipient_hash"]: p["pack_id"] for p in rig.cloud_packs(cid)}
    ids = [by_hash[rig.hashlib.sha256(f"f{i}@demo.example".encode()).hexdigest()] for i in range(3)]
    # Mock the dispatcher's write; running the real dispatcher would require Resend.
    rig.dynamo.update_pack(ids[0], status="sent", resend_message_id="re-1")
    assert rig.cloud.delete(f"/packs/{ids[1]}").json() == {"cancelled": True}
    assert rig.dynamo.claim_pack(ids[2]) is True
    rig.approve(cid)
    assert len(rig.cloud_packs(cid)) == 5
    def _recipient(email):
        return rig.sql("SELECT cr.status, cr.message_id, j.status FROM campaign_recipients cr "
                       "JOIN contacts c ON c.id=cr.contact_id "
                       "JOIN dispatched_job j ON j.campaign_id=cr.campaign_id AND j.recipient_id=c.id "
                       "WHERE cr.campaign_id=? AND c.email=?", (cid, email))
    assert _recipient("f0@demo.example") == [("sent", "re-1", "completed")]
    assert _recipient("f1@demo.example")[0][::2] == ("failed", "failed")
    assert _recipient("f2@demo.example")[0][::2] == ("sent", "dispatched")
    assert rig.sql("SELECT COUNT(*) FROM audit_event WHERE action='pack.outcome_failed' AND target=?",
                   (f"pack:{ids[1]}",)) == [(1,)]


def test_stage_retry_dedupes(rig):
    cid = rig.campaign(rig.c, 3, "t")
    tid, sid = rig.stage(cid, 1)
    rig.fail_record_once()
    with pytest.raises(Exception, match="database is locked \\(injected\\)"):
        rig.asyncio.run(rig.launch.launch_campaign(cid, template_id_override=tid, stage_id=sid))
    rig.asyncio.run(rig.launch.launch_campaign(cid, template_id_override=tid, stage_id=sid))
    rig.one_each(cid)


def test_next_stage_after_unrecorded_stage_sends(rig):
    cid = rig.campaign(rig.c, 3, "u")
    t1, s1 = rig.stage(cid, 1)
    t2, s2 = rig.stage(cid, 2)
    rig.fail_record_once()
    with pytest.raises(Exception, match="database is locked \\(injected\\)"):
        rig.asyncio.run(rig.launch.launch_campaign(cid, template_id_override=t1, stage_id=s1))
    rig.asyncio.run(rig.launch.launch_campaign(cid, template_id_override=t2, stage_id=s2))
    held = rig.cloud_packs(cid)
    assert len(held) == 6
    hashes = {p["recipient_hash"] for p in held}
    assert len(hashes) == 3
    assert all(len({p["content_hash"] for p in held if p["recipient_hash"] == h}) == 2 for h in hashes)


def test_concurrent_approve_next_one_pack_each(rig):
    cid = rig.campaign(rig.c, 5, "c")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    push = rig.launch._push_packs
    async def _both():
        arrived, barrier = 0, rig.asyncio.Event()
        async def _push(packs, bearer):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                barrier.set()
            await barrier.wait()
            return await push(packs, bearer)
        rig.monkeypatch.setattr(rig.launch, "_push_packs", _push)
        return await rig.asyncio.gather(rig.launch.dispatch_batch(cid, 1),
                                        rig.launch.dispatch_batch(cid, 1), return_exceptions=True)
    results = rig.asyncio.run(_both())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, rig.IntegrityError) for r in results) == 1
    rig.one_each(cid)


def test_pack_id_deterministic_and_keyed(rig):
    args = (1, "2026-01-01 00:00:00", "main", 2, 0)
    x = rig.launch._pack_id(*args)
    assert rig.launch._pack_id(*args) == x
    assert rig.uuid.UUID(x).version == 5
    for i, value in enumerate((2, "2026-01-02 00:00:00", "stage:1", 3, 1)):
        changed = list(args)
        changed[i] = value
        assert rig.launch._pack_id(*changed) != x, i


def test_seed_packs_keep_random_ids(rig):
    cid = rig.campaign(rig.c, 5, "e")
    rig.sql("INSERT INTO seed_addresses(email) VALUES ('seed@demo.example')")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    rig.approve(cid)
    held = rig.cloud_packs(cid)
    seed_hash = rig.hashlib.sha256(b"seed@demo.example").hexdigest()
    seeds = [p for p in held if p["recipient_hash"] == seed_hash]
    contacts = [p for p in held if p["recipient_hash"] != seed_hash]
    assert len(seeds) == 2
    assert len({p["pack_id"] for p in seeds}) == 2
    assert all(rig.uuid.UUID(p["pack_id"]).version == 4 for p in seeds)
    assert len(contacts) == len({p["recipient_hash"] for p in contacts}) == 5
    assert {p["pack_id"] for p in contacts} == {j[0] for j in rig.sql(
        "SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}


def test_push_reports_held_status(rig):
    pid = str(rig.uuid.uuid4())
    body = [{"pack_id": pid, "campaign_id": 1, "recipient_hash": "r", "content_hash": "c",
             "send_at": "2099-01-01T00:00:00Z", "key_id": "test", "encrypted_blob": rig.base64.b64encode(b"test").decode()}]
    first = rig.cloud.post("/packs", json=body)
    assert first.status_code == 200
    assert first.json()["held"] == []
    second = rig.cloud.post("/packs", json=body)
    assert second.status_code == 200
    assert second.json()["accepted"] == 0
    assert second.json()["held"] == [{"pack_id": pid, "status": "pending",
                                      "resend_message_id": None, "failure_reason": None}]


def test_stress_random_faults_retry_to_one_pack_each(rig):
    for seed in range(12):
        rng = rig.random.Random(seed)
        n = rng.randint(3, 9)
        cid = rig.campaign(rig.c, n, f"s{seed}x")
        rig.monkeypatch.setattr(rig.launch, "PACK_PUSH_CHUNK", rng.choice([1, 2, 3]))
        for attempt in range(4):
            rig.clear_faults()
            if attempt < 3:
                kind = rng.choice(("put", "record", "none"))
                at = rng.randint(1, n)
                if kind == "put":
                    rig.fault_put(at)
                elif kind == "record":
                    rig.fail_record_once()
            if attempt:
                pending = rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,))
                queued = rig.sql("SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id=? AND status='queued'", (cid,))
                if pending != [("pending",)] or queued == [(0,)]:
                    break
            path = f"/admin/campaigns/{cid}/launch" if attempt == 0 else f"/admin/campaigns/{cid}/batches/1/approve"
            r = rig.c.post(path)
            assert r.status_code in (200, 400, 500), (seed, attempt, r.text[:300])
            cloud_ids = {p["pack_id"] for p in rig.cloud_packs(cid)}
            recorded_ids = {j[0] for j in rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}
            assert len(cloud_ids - recorded_ids) <= n, (seed, attempt)
            if r.status_code == 200:
                assert cloud_ids - recorded_ids == set(), (seed, attempt)
                break
        rig.one_each(cid)


def test_interrupted_launch_then_relaunch_finishes_batch(rig):
    cid = rig.campaign(rig.c, 5, "t14")
    rig.fault_put(3)
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("cloud /packs rejected (500) on chunk 2")
    assert rig.sql("SELECT status, COUNT(*) FROM campaign_recipients WHERE campaign_id=? GROUP BY status",
                   (cid,)) == [("queued", 3), ("sent", 2)]
    assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("pending",)]
    cloud_ids = {p["pack_id"] for p in rig.cloud_packs(cid)}
    assert len(cloud_ids) == 2
    assert cloud_ids == {j[0] for j in rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}
    assert rig.sql("SELECT dispatch_started_at IS NOT NULL FROM campaign_batches WHERE campaign_id=?", (cid,)) == [(1,)]
    page = rig.c.get(f"/admin/campaigns/{cid}/analytics")
    assert page.status_code == 200
    assert f"approveBatch({cid},1)" in page.text
    page = rig.c.get("/admin/campaigns")
    assert page.status_code == 200
    assert 'data-seq="0" data-old-badge="' in page.text
    assert "if(d.note)alert(d.note);" in page.text
    assert "expected_seq:" in page.text
    assert "if(relaunch){alert(d.detail);_reloadKeepScroll();return;}" in page.text
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "finish-batch"
    assert r.json()["dispatched"] == 3 and r.json()["note"]
    rig.one_each(cid)
    assert rig.sql("SELECT status, dispatch_started_at FROM campaign_batches WHERE campaign_id=?", (cid,)) == [("sending", None)]


def test_partial_branch_applies_held_outcomes(rig):
    cid = rig.campaign(rig.c, 5, "t15")
    rig.fault_put(3)
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 400
    held = rig.cloud_packs(cid)
    assert len(held) == 2
    assert rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,)) == []
    pid = held[0]["pack_id"]
    # Mock the dispatcher's resolved write inside the first acknowledged chunk.
    rig.dynamo.update_pack(pid, status="sent", resend_message_id="re-t15")
    rig.clear_faults()
    rig.fault_put(3)
    r = rig.c.post(f"/admin/campaigns/{cid}/batches/1/approve")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("cloud /packs rejected (500) on chunk 2")
    assert {p["pack_id"] for p in rig.cloud_packs(cid)} == {p["pack_id"] for p in held}
    assert {j[0] for j in rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))} == {
        p["pack_id"] for p in held}
    assert rig.sql("SELECT cr.status, cr.message_id, j.status FROM campaign_recipients cr "
                   "JOIN dispatched_job j ON j.campaign_id=cr.campaign_id AND j.recipient_id=cr.contact_id "
                   "WHERE j.job_id=?", (pid,)) == [("sent", "re-t15", "completed")]
    assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("pending",)]
    rig.clear_faults()
    rig.approve(cid)
    rig.one_each(cid)


def test_partial_record_failure_keeps_original_error(rig):
    cid = rig.campaign(rig.c, 5, "t16")
    rig.fault_put(3)
    rig.fail_record_once()
    r = rig.c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("cloud /packs rejected (500) on chunk 2:")
    assert len(rig.cloud_packs(cid)) == 2
    assert rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,)) == []
    assert rig.sql("SELECT status FROM campaign_recipients WHERE campaign_id=?", (cid,)) == [("queued",)] * 5
    rig.clear_faults()
    rig.approve(cid)
    rig.one_each(cid)


def test_held_not_in_cloud_ack(rig):
    import json

    keys = {"accepted", "submitted", "enqueued", "deferred"}
    for mode in ("approve", "relaunch"):
        cid = rig.campaign(rig.c, 5, f"t17{mode}")
        rig.fail_record_once()
        assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
        rig.clear_faults()
        if mode == "approve":
            summary = rig.approve(cid)
            action, target = "batch.approved", f"campaign:{cid}:batch:1"
        else:
            r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
            assert r.status_code == 200
            summary = r.json()
            action, target = "campaign.launched", f"campaign:{cid}"
        assert set(summary["cloud_ack"]) == keys
        assert summary["cloud_ack"]["accepted"] == 0
        assert summary["cloud_ack"]["submitted"] == 5
        rows = rig.sql("SELECT details_json FROM audit_event WHERE action=? AND target=?", (action, target))
        assert len(rows) == 1
        assert set(json.loads(rows[0][0])["cloud_ack"]) == keys
        assert json.loads(rows[0][0])["cloud_ack"] == summary["cloud_ack"]
        rig.one_each(cid)


def test_held_outcome_failure_does_not_500(rig, caplog):
    from mailman.terminal import sync_client

    cid = rig.campaign(rig.c, 5, "t18")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    rig.clear_faults()
    pid = rig.cloud_packs(cid)[0]["pack_id"]
    rig.dynamo.update_pack(pid, status="sent", resend_message_id="re-t18")
    apply_outcome = sync_client._apply_pack_outcome
    calls = 0
    def _fail_once(pack):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("held outcome failed (injected)")
        return apply_outcome(pack)
    rig.monkeypatch.setattr(sync_client, "_apply_pack_outcome", _fail_once)
    with caplog.at_level("ERROR", logger="terminal.launch"):
        r = rig.c.post(f"/admin/campaigns/{cid}/batches/1/approve")
    assert r.status_code == 200, r.text[:300]
    assert calls == 1
    assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("sending",)]
    assert any(rec.name == "terminal.launch" and rec.levelname == "ERROR"
               and "recorded, but applying held outcomes failed" in rec.getMessage()
               and pid in rec.getMessage() for rec in caplog.records)
    rig.one_each(cid)


def test_interrupted_relaunch_then_relaunch_finishes_generation(rig):
    cid = rig.campaign(rig.c, 5, "t19")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    rig.fault_put(4)
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("cloud /packs rejected (500) on chunk 2")
    assert len(rig.cloud_packs(cid)) == 8
    assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(1,)]
    assert rig.c.post(f"/admin/campaigns/{cid}/pause").status_code == 200
    assert rig.c.post(f"/admin/campaigns/{cid}/resume").json()["status"] == "tested"
    page = rig.c.get("/admin/campaigns")
    assert page.status_code == 200
    assert f'onclick="launchCampaign({cid},this,true)">Re-launch</button>' in page.text
    assert 'data-seq="' in page.text
    rig.clear_faults()
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "finish-relaunch" and r.json()["dispatched"] == 3
    assert r.json()["note"].startswith("Finished the interrupted re-launch: 2 recipient(s)")
    assert len(rig.cloud_packs(cid)) == 10
    assert len(rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))) == 10
    assert set(rig.per_gen_live(cid).values()) == {1}
    assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(0,)]
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "relaunch"
    assert r.json()["generation"] == 2 and len(rig.cloud_packs(cid)) == 15


def test_suppressed_queued_contact_does_not_block_relaunch(rig):
    rig.settings.set("send_batch_size", 2)
    cid = rig.campaign(rig.c, 4, "t20")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    h = rig.hashlib.sha256(b"t202@demo.example").hexdigest()
    rig.sql("INSERT INTO suppression_hashes(email_hash, reason, scope) VALUES (?, 'manual', 'all')", (h,))
    for _ in range(2):
        r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "relaunch" and r.json()["dispatched"] == 3
        assert rig.sql("SELECT cr.status FROM campaign_recipients cr JOIN contacts c ON c.id=cr.contact_id "
                       "WHERE cr.campaign_id=? AND c.email='t202@demo.example'", (cid,)) == [("queued",)]


def _interrupted_batch_n2(rig, tag):
    rig.settings.set("send_batch_size", 3)
    cid = rig.campaign(rig.c, 6, tag)
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    rig.sql("UPDATE campaign_batches SET status='drained', approve_unlock_at=NULL "
            "WHERE campaign_id=? AND batch_no=1", (cid,))
    rig.fault_put(3)
    r = rig.c.post(f"/admin/campaigns/{cid}/batches/2/approve")
    assert r.status_code == 400
    assert rig.sql("SELECT status, dispatch_started_at IS NOT NULL FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=2", (cid,)) == [("pending", 1)]
    assert len(rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))) == 5
    rig.clear_faults()
    return cid


def test_interrupted_batch_n2_approve_reachable(rig):
    cid = _interrupted_batch_n2(rig, "t21")
    page = rig.c.get(f"/admin/campaigns/{cid}/analytics")
    assert page.status_code == 200 and f"approveBatch({cid},2)" in page.text
    assert rig.c.post(f"/admin/campaigns/{cid}/batches/2/approve").status_code == 200
    rig.one_each(cid)


def test_interrupted_batch_n2_relaunch_finishes(rig):
    cid = _interrupted_batch_n2(rig, "t22")
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "finish-batch" and r.json()["batch_no"] == 2
    rig.one_each(cid)
    assert rig.sql("SELECT approved_at IS NOT NULL FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=2", (cid,)) == [(1,)]
    assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("done",)]


def test_retry_failed_launch_interrupted_then_relaunch(rig):
    cid = rig.campaign(rig.c, 3, "t23")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    mapped = rig.gen_of(cid)
    pid = next(iter(mapped))
    contact = mapped[pid].contact_id
    rig.fail_pack(pid)
    assert rig.c.post(f"/admin/campaigns/{cid}/retry").json()["reset"] == 1
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    assert rig.sql("SELECT status, dispatch_started_at IS NOT NULL FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=1", (cid,)) == [("sending", 1)]
    rig.clear_faults()
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "finish-batch"
    assert len(rig.cloud_packs(cid)) == 4
    assert {p.attempt for p in rig.gen_of(cid).values() if p.contact_id == contact} == {0, 1}
    assert {p["pack_id"] for p in rig.cloud_packs(cid)} == {j[0] for j in rig.sql(
        "SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}


def test_cancel_then_relaunch_starts_new_generation(rig):
    import json

    cid = rig.campaign(rig.c, 5, "t24")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    rig.fault_put(3)
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 400
    rig.clear_faults()
    rig.monkeypatch.setenv("CLOUD_URL", "http://cloud.test")
    r = rig.c.post(f"/admin/campaigns/{cid}/cancel")
    assert r.status_code == 200 and r.json()["packs_cancelled"] == 7
    assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(0,)]
    details = rig.sql("SELECT details_json FROM audit_event WHERE action='campaign.cancelled' AND target=?",
                      (f"campaign:{cid}",))
    assert json.loads(details[0][0])["markers_cleared"] is True
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "relaunch" and r.json()["generation"] == 2
    live = [p for p in rig.cloud_packs(cid) if p["status"] == "pending"]
    assert len(live) == len({p["recipient_hash"] for p in live}) == 5


def test_double_click_relaunch_one_generation(rig):
    cid = rig.campaign(rig.c, 5, "t25")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    push = rig.launch._push_packs
    async def _both():
        arrived, barrier = 0, rig.asyncio.Event()
        async def _push(packs, bearer):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                barrier.set()
            await rig.asyncio.wait_for(barrier.wait(), 2)
            return await push(packs, bearer)
        rig.monkeypatch.setattr(rig.launch, "_push_packs", _push)
        return await rig.asyncio.gather(rig.launch.launch_campaign(cid, relaunch=True),
                                        rig.launch.launch_campaign(cid, relaunch=True), return_exceptions=True)
    results = rig.asyncio.run(_both())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, rig.IntegrityError) for r in results) == 1
    assert len(rig.cloud_packs(cid)) == 10
    assert rig.sql("SELECT relaunch_gen, relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(1, 0)]


def test_partial_branch_outcome_guard_alone(rig, caplog):
    cid = rig.campaign(rig.c, 5, "t26")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    assert len(rig.cloud_packs(cid)) == 5
    assert rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,)) == []
    rig.clear_faults()
    # The fault wrapper counts before put_pack raises PackAlreadyExists: duplicates count.
    rig.fault_put(3)
    def _fail_outcomes(held):
        raise RuntimeError("held outcomes failed (injected)")
    rig.monkeypatch.setattr(rig.launch, "_apply_held_outcomes", _fail_outcomes)
    with caplog.at_level("ERROR", logger="terminal.launch"):
        r = rig.c.post(f"/admin/campaigns/{cid}/batches/1/approve")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("cloud /packs rejected (500) on chunk 2")
    recorded = {j[0] for j in rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}
    mapped = rig.gen_of(cid)
    contacts = [r[0] for r in rig.sql(
        "SELECT contact_id FROM campaign_recipients WHERE campaign_id=? ORDER BY contact_id", (cid,))]
    assert recorded == {pid for pid, p in mapped.items() if p.contact_id in contacts[:2]}
    assert len(recorded) == 2
    assert any(rec.name == "terminal.launch" and rec.levelname == "ERROR"
               and "acknowledged pack(s) recorded, but applying held outcomes failed" in rec.getMessage()
               and all(pid in rec.getMessage() for pid in recorded) for rec in caplog.records)


def test_held_outcomes_applied_per_pack(rig, caplog):
    from mailman.terminal import sync_client

    cid = rig.campaign(rig.c, 5, "t27")
    rig.fail_record_once()
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    rig.clear_faults()
    resolved = sorted(rig.cloud_packs(cid), key=lambda p: rig.gen_of(cid)[p["pack_id"]].contact_id)[:2]
    ids = [p["pack_id"] for p in resolved]
    for pid in ids:
        rig.dynamo.update_pack(pid, status="sent", resend_message_id=f"re-{pid}")
    apply_outcome = sync_client._apply_pack_outcome
    calls = []
    def _fail_once(pack):
        calls.append(pack["pack_id"])
        if len(calls) == 1:
            raise RuntimeError("first held outcome failed (injected)")
        return apply_outcome(pack)
    rig.monkeypatch.setattr(sync_client, "_apply_pack_outcome", _fail_once)
    with caplog.at_level("ERROR", logger="terminal.launch"):
        r = rig.c.post(f"/admin/campaigns/{cid}/batches/1/approve")
    assert r.status_code == 200, r.text
    assert calls == ids
    assert rig.sql("SELECT status FROM dispatched_job WHERE job_id=?", (ids[0],)) == [("dispatched",)]
    assert rig.sql("SELECT status FROM dispatched_job WHERE job_id=?", (ids[1],)) == [("completed",)]
    errors = [rec.getMessage() for rec in caplog.records if rec.name == "terminal.launch" and rec.levelname == "ERROR"]
    assert any(ids[0] in message and ids[1] not in message for message in errors)


def test_0019_upgrade_keeps_inflight_campaign(tmp_path, monkeypatch):
    import sqlite3
    from mailman.terminal import db
    from fixtures import orm_levels

    p = tmp_path / "level18.sqlite"
    orm_levels.make_db_at_level("terminal", p, 18)
    with sqlite3.connect(str(p)) as con:
        tid = con.execute("SELECT id FROM templates ORDER BY id LIMIT 1").fetchone()[0]
        cid = con.execute("INSERT INTO campaigns(name, template_id, kind, status) "
                          "VALUES ('Inflight upgrade', ?, 'transactional', 'dispatched')", (tid,)).lastrowid
        con.execute("INSERT INTO campaign_batches(campaign_id, batch_no, size, status) "
                    "VALUES (?, 1, 1, 'pending')", (cid,))
    monkeypatch.setattr(db, "DB_PATH", p)
    db.init()
    with sqlite3.connect(str(p)) as con:
        assert con.execute("SELECT status, relaunch_gen, relaunch_open, relaunch_seq FROM campaigns "
                           "WHERE id=?", (cid,)).fetchall() == [("dispatched", 0, 0, 0)]
        assert con.execute("SELECT status, dispatch_started_at FROM campaign_batches "
                           "WHERE campaign_id=?", (cid,)).fetchall() == [("pending", None)]
        assert ("0019_send_generation.sql",) in con.execute("SELECT name FROM _migration").fetchall()


def test_approve_after_interrupted_relaunch_uses_current_generation(rig):
    rig.settings.set("send_batch_size", 2)
    cid = rig.campaign(rig.c, 4, "t30")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    rig.sql("UPDATE campaign_batches SET status='drained', approve_unlock_at=NULL "
            "WHERE campaign_id=? AND batch_no=1", (cid,))
    rig.fault_put(4)
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 400
    contacts = [r[0] for r in rig.sql(
        "SELECT contact_id FROM campaign_recipients WHERE campaign_id=? ORDER BY contact_id", (cid,))]
    assert rig.sql("SELECT status FROM campaign_recipients WHERE campaign_id=? "
                   "AND contact_id IN (?, ?) ORDER BY contact_id", (cid, *contacts[2:])) == [("queued",)] * 2
    rig.clear_faults()
    assert rig.c.post(f"/admin/campaigns/{cid}/batches/2/approve").status_code == 200
    assert len(rig.cloud_packs(cid)) == 6
    assert all(n <= 1 for n in rig.per_gen_live(cid).values())
    assert {p.gen_key for p in rig.gen_of(cid).values() if p.contact_id in contacts[2:]} == {"relaunch:1"}
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "finish-relaunch" and r.json()["dispatched"] == 0
    assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(0,)]
    assert len(rig.cloud_packs(cid)) == 6
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["generation"] == 2
    assert len(rig.cloud_packs(cid)) == 10


def test_retry_then_launch_after_interrupted_relaunch(rig):
    cid = rig.campaign(rig.c, 4, "t30b")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    contacts = [r[0] for r in rig.sql(
        "SELECT contact_id FROM campaign_recipients WHERE campaign_id=? ORDER BY contact_id", (cid,))]
    pid = next(pid for pid, p in rig.gen_of(cid).items() if p.contact_id == contacts[2])
    rig.fail_pack(pid)
    rig.fault_put(4)
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 400
    rig.clear_faults()
    assert rig.c.post(f"/admin/campaigns/{cid}/retry").json()["reset"] == 1
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    assert len(rig.cloud_packs(cid)) == 7
    assert sum(p.gen_key == "relaunch:1" and p.contact_id == contacts[2] for p in rig.gen_of(cid).values()) == 1
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "finish-relaunch" and r.json()["dispatched"] == 1
    assert len(rig.cloud_packs(cid)) == 8
    assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(0,)]


def test_resume_then_launch_after_interrupted_relaunch(rig):
    cid = rig.campaign(rig.c, 4, "t30c")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    rig.fault_put(4)
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 400
    rig.clear_faults()
    assert rig.c.post(f"/admin/campaigns/{cid}/pause").status_code == 200
    assert rig.c.post(f"/admin/campaigns/{cid}/resume").json()["status"] == "tested"
    r = rig.c.post("/admin/contacts", json={"email": "t30cy@demo.example", "name": "Y", "consent_source": "demo"})
    assert r.status_code in (200, 201)
    contact = r.json()["id"]
    rig.sql("INSERT INTO campaign_recipients(campaign_id, contact_id, status) VALUES (?, ?, 'queued')", (cid, contact))
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    assert [(p.gen_key, p.attempt) for p in rig.gen_of(cid).values() if p.contact_id == contact] == [("relaunch:1", 0)]
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "finish-relaunch" and r.json()["dispatched"] == 2
    assert r.json()["cloud_ack"]["accepted"] == 1
    assert all(n <= 1 for n in rig.per_gen_live(cid).values())
    contacts = [r[0] for r in rig.sql(
        "SELECT contact_id FROM campaign_recipients WHERE campaign_id=? ORDER BY contact_id", (cid,))]
    mapped = rig.gen_of(cid)
    assert all(sum(p.gen_key == "relaunch:1" and p.contact_id == c for p in mapped.values()) == 1
               for c in contacts[2:4])


def test_stale_relaunch_seq(rig):
    cid = rig.campaign(rig.c, 5, "t31")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    assert rig.sql("SELECT relaunch_seq FROM campaigns WHERE id=?", (cid,)) == [(0,)]
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch", json={"expected_seq": 0}).status_code == 200
    assert rig.sql("SELECT relaunch_gen, relaunch_seq FROM campaigns WHERE id=?", (cid,)) == [(1, 1)]
    assert len(rig.cloud_packs(cid)) == 10
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch", json={"expected_seq": 0})
    assert r.status_code == 409 and r.json()["detail"] == rig.launch._STALE_RELAUNCH
    assert len(rig.cloud_packs(cid)) == 10
    assert rig.sql("SELECT relaunch_gen, relaunch_seq FROM campaigns WHERE id=?", (cid,)) == [(1, 1)]
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch", json={"expected_seq": 1}).status_code == 200
    assert rig.sql("SELECT relaunch_gen, relaunch_seq FROM campaigns WHERE id=?", (cid,)) == [(2, 2)]
    page = rig.c.get("/admin/campaigns")
    assert page.status_code == 200
    assert 'data-seq="2" data-old-badge="' in page.text and "expected_seq:" in page.text


def test_two_finish_presses_from_stale_page(rig):
    for case in ("relaunch", "batch"):
        cid = rig.campaign(rig.c, 5, f"t31b{case}")
        if case == "relaunch":
            assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
        rig.fault_put(4)
        path = "relaunch" if case == "relaunch" else "launch"
        assert rig.c.post(f"/admin/campaigns/{cid}/{path}").status_code == 400
        rig.clear_faults()
        seq = 1 if case == "relaunch" else 0
        page = rig.c.get("/admin/campaigns")
        assert page.status_code == 200 and f'data-seq="{seq}"' in page.text
        r = rig.c.post(f"/admin/campaigns/{cid}/relaunch", json={"expected_seq": seq})
        assert r.status_code == 200 and r.json()["mode"] == f"finish-{case}"
        ids = {p["pack_id"] for p in rig.cloud_packs(cid)}
        assert len(ids) == (10 if case == "relaunch" else 5)
        r = rig.c.post(f"/admin/campaigns/{cid}/relaunch", json={"expected_seq": seq})
        assert r.status_code == 409 and r.json()["detail"] == rig.launch._STALE_RELAUNCH
        assert {p["pack_id"] for p in rig.cloud_packs(cid)} == ids
        assert rig.sql("SELECT relaunch_gen, relaunch_open, relaunch_seq FROM campaigns WHERE id=?", (cid,)) == [
            (1 if case == "relaunch" else 0, 0, seq + 1)]


def test_nonconfirmed_cancels_keep_markers(rig):
    for case in ("no_cloud", "no_bearer", "inflight"):
        cid = rig.campaign(rig.c, 5, f"t32{case}")
        assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
        rig.fault_put(4)
        assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 400
        rig.clear_faults()
        with rig.monkeypatch.context() as m:
            if case == "no_cloud":
                m.delenv("CLOUD_URL", raising=False)
            else:
                m.setenv("CLOUD_URL", "http://cloud.test")
            if case == "no_bearer":
                (rig.keys / "cloud_bearer.txt").unlink()
            if case == "inflight":
                m.setitem(rig.launch._PUSHING, cid, 1)
            try:
                assert rig.c.post(f"/admin/campaigns/{cid}/cancel").status_code == 200
                assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(1,)]
            finally:
                if case == "no_bearer":
                    (rig.keys / "cloud_bearer.txt").write_text("tok")
        r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
        assert r.status_code == 200 and r.json()["mode"] == "finish-relaunch" and r.json()["generation"] == 1
        assert all(n <= 1 for n in rig.per_gen_live(cid).values())


def test_suppression_in_batch_finish(rig):
    cid = rig.campaign(rig.c, 5, "t33")
    rig.fault_put(4)
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 400
    rig.clear_faults()
    h = rig.hashlib.sha256(b"t334@demo.example").hexdigest()
    rig.sql("INSERT INTO suppression_hashes(email_hash, reason, scope) VALUES (?, 'unsubscribe', 'all')", (h,))
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "finish-batch"
    assert r.json()["dispatched"] == 2 and r.json()["suppressed"] == 1
    assert r.json()["note"].startswith("Finished the interrupted send of batch 1: 1 recipient(s)")
    assert all(p["recipient_hash"] != h for p in rig.cloud_packs(cid))
    assert rig.sql("SELECT cr.status, cr.failure_reason FROM campaign_recipients cr JOIN contacts c "
                   "ON c.id=cr.contact_id WHERE cr.campaign_id=? AND c.email='t334@demo.example'", (cid,)) == [
        ("cancelled", "suppressed: unsubscribe")]


def test_zero_to_send_finish_drains_and_unlocks(rig):
    import sqlite3
    from mailman.terminal import sync_client

    rig.settings.set("send_batch_size", 2)
    cid = rig.campaign(rig.c, 4, "t34")
    mark_loaded = rig.launch._mark_batch_loaded
    calls = 0
    def _fail_once(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("marker clear failed (injected)")
        return mark_loaded(*args, **kw)
    rig.monkeypatch.setattr(rig.launch, "_mark_batch_loaded", _fail_once)
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 500
    assert rig.sql("SELECT status, dispatch_started_at IS NOT NULL FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=1", (cid,)) == [("pending", 1)]
    assert len(rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))) == 2
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200 and r.json()["mode"] == "finish-batch" and r.json()["dispatched"] == 0
    assert r.json()["note"].startswith("Batch 1 had already")
    assert rig.sql("SELECT status, window_end != '', dispatch_started_at FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=1", (cid,)) == [("sending", 1, None)]
    rig.sql("UPDATE campaign_batches SET window_end='1970-01-01T00:00:00Z' WHERE campaign_id=? AND batch_no=1", (cid,))
    sync_client._drain_batches()
    assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) == [("drained",)]
    rig.sql("UPDATE campaign_batches SET approve_unlock_at=NULL WHERE campaign_id=? AND batch_no=1", (cid,))
    assert rig.c.post(f"/admin/campaigns/{cid}/batches/2/approve").status_code == 200


def test_one_send_choke_point():
    import ast
    from pathlib import Path
    from mailman.terminal import launch

    tree = ast.parse(Path(launch.__file__).read_text())
    found = {"_pack_id": [], "_push_packs": []}
    def _visit(node, owner=None):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in found:
            found[node.func.id].append((owner, node.lineno))
        for child in ast.iter_child_nodes(node):
            _visit(child, owner)
    _visit(tree)
    for called, owner in (("_pack_id", "_plan_sends"), ("_push_packs", "_push_and_record")):
        assert found[called]
        assert all(where == owner for where, _ in found[called]), (called, found[called])


# Seeds 0-3 plus the first failing seed for each negative case: 4, 12 and 0.
FIXED_SEEDS = [0, 1, 2, 3, 4, 12]


def _walk_500_allowed(status: int, fired: int) -> bool:
    """A 500 is tolerated only on a step where an injected fault actually fired."""
    return status != 500 or fired > 0


def _walk_sequential_ok(status: int, detail, press_text: str) -> bool:
    """A sequential press can never lose a race, so it must not get the concurrent-press 409."""
    return not (status == 409 and isinstance(detail, str) and detail.startswith(press_text))


def _walk_send_paths(rig, seed, steps):
    import re
    import sqlite3
    from types import SimpleNamespace

    rng = rig.random.Random(seed)
    n = rng.randint(3, 6)
    rig.settings.set("send_batch_size", rng.choice([2, 3, 1000]))
    rig.monkeypatch.setattr(rig.launch, "PACK_PUSH_CHUNK", rng.choice([1, 2, 3]))
    cid = rig.campaign(rig.c, n, f"w{seed}x")
    assert rig.launch._active_seed_emails() == [], (seed, "walk requires zero active seeds")
    contacts = rig.sql("SELECT c.id, c.email FROM contacts c JOIN campaign_recipients cr "
                       "ON cr.contact_id=c.id WHERE cr.campaign_id=? ORDER BY c.id", (cid,))
    model, suppression_snap, history = SimpleNamespace(unfinished=False), {}, []
    stage_number = 0
    base = f"/admin/campaigns/{cid}"

    def _message(letter, detail):
        return f"seed={seed} step={step} action={action} invariant=({letter}) {detail}; history={history!r}"

    def _state():
        op, gen, seq = rig.sql("SELECT relaunch_open, relaunch_gen, relaunch_seq FROM campaigns WHERE id=?", (cid,))[0]
        batches = tuple(rig.sql("SELECT batch_no, dispatch_started_at FROM campaign_batches "
                                "WHERE campaign_id=? ORDER BY batch_no", (cid,)))
        return op, gen, seq, batches

    def _unfinished():
        op, _, _, batches = _state()
        return op == 1 or any(marker is not None for _, marker in batches)

    def _ids():
        return {p["pack_id"] for p in rig.cloud_packs(cid)}

    def _mapped():
        try:
            return rig.gen_of(cid)
        except AssertionError as exc:
            raise AssertionError(_message("a", str(exc))) from exc

    def _live():
        _mapped()
        assert all(count <= 1 for count in rig.per_gen_live(cid).values()), _message("a", "two live packs in one generation")

    def _keys(before):
        gen = _state()[1]
        mapped = _mapped()
        for pid in _ids() - before:
            key = mapped[pid].gen_key
            if not key.startswith("stage:"):
                assert key == rig.launch._gen_key_for(gen), _message("e", f"new pack uses {key}, current generation {gen}")

    def _finish_rule(pre, gen):
        now_gen = _state()[1]
        if pre:
            assert now_gen == gen, _message("d", "a finish press opened a generation")
        else:
            assert now_gen <= gen + 1, _message("d", "one press opened two generations")

    def _response(r):
        history.append((r.status_code, r.text[:180]))
        assert r.status_code in (200, 400, 409, 500), _message("a", f"unexpected HTTP {r.status_code}")
        assert _walk_500_allowed(r.status_code, rig.faults_fired()), _message("a", "500 with no fired fault")
        try:
            detail = r.json().get("detail")
        except (ValueError, AssertionError):
            detail = None
        assert _walk_sequential_ok(r.status_code, detail, rig.launch._CONCURRENT_PRESS), _message("a", "concurrent-press 409 on a sequential call")

    def _press(body=None):
        before, pre, gen = _ids(), model.unfinished, _state()[1]
        r = rig.c.post(base + "/relaunch", json=body) if body is not None else rig.c.post(base + "/relaunch")
        _response(r)
        _finish_rule(pre, gen)
        _keys(before)
        model.unfinished = _unfinished()
        return r

    def _table_snapshot():
        rows, kw = {}, {}
        while True:
            page = rig.dynamo._get_table().scan(**kw)
            rows.update(((p["pk"], p["sk"]), p) for p in page["Items"])
            if "LastEvaluatedKey" not in page:
                return rows
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _settle_fork():
        if model.unfinished:
            page = rig.c.get("/admin/campaigns")
            assert page.status_code == 200, _message("c", "campaign page failed")
            status = rig.sql("SELECT status FROM campaigns WHERE id=?", (cid,))[0][0]
            if status == "paused":
                assert f"campAction({cid},'resume')" in page.text, _message("c", "missing Resume button")
            else:
                pattern = rf'data-seq="\d+" data-old-badge="[01]" onclick="launchCampaign\({cid},this,true\)">Re-launch</button>'
                assert re.search(pattern, page.text), _message("c", "missing Re-launch button for unfinished work")
        snapshot = rig.db_path.with_name("walk-snapshot.sqlite")
        with sqlite3.connect(str(rig.db_path)) as live, sqlite3.connect(str(snapshot)) as snap:
            live.backup(snap)
        table_before, unfinished_before = _table_snapshot(), model.unfinished
        try:
            for _ in range(3):
                if not _unfinished():
                    break
                # The fork checks each finish press before updating the model flag.
                before, pre, gen = _ids(), model.unfinished, _state()[1]
                r = rig.c.post(base + "/relaunch")
                history.append(("settle", r.status_code, r.text[:180]))
                _finish_rule(pre, gen)
                _keys(before)
                model.unfinished = _unfinished()
                assert r.status_code == 200, _message("c", "unfinished send cannot be settled")
            assert not _unfinished(), _message("c", "markers remain after three finish presses")
            _live()
        finally:
            with sqlite3.connect(str(snapshot)) as snap, sqlite3.connect(str(rig.db_path)) as live:
                snap.backup(live)
            table = rig.dynamo._get_table()
            current = _table_snapshot()
            for key in current.keys() - table_before.keys():
                table.delete_item(Key={"pk": key[0], "sk": key[1]})
            for key, item in table_before.items():
                if current.get(key) != item:
                    table.put_item(Item=item)
            model.unfinished = unfinished_before

    for step in range(steps):
        rig.clear_faults()
        pending = rig.c.get(base + "/batches").json()["next_pending"]
        recorded = {j[0] for j in rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,))}
        live_recorded = [p for p in rig.cloud_packs(cid) if p["status"] == "pending" and p["pack_id"] in recorded]
        unsuppressed = [(contact, email) for contact, email in contacts if contact not in suppression_snap]
        enabled = ["launch", "relaunch", "resume_then_launch", "cancel_up", "cancel_down", "stage", "double_click"]
        if pending is not None:
            enabled.append("approve")
        if live_recorded:
            enabled.extend(("retry_then_launch", "outcome_fail"))
        if unsuppressed:
            enabled.append("suppress")
        action = rng.choice(enabled)
        before, snap_before = _ids(), _state()
        history.append((step, action))
        faulted = action not in ("cancel_up", "cancel_down") and rng.random() < 0.4
        fault_kind = None
        if faulted:
            fault_kind = rng.choice(("put", "record"))
            if fault_kind == "put":
                at = rng.randint(1, n + 1)
                rig.fault_put(at)
                history.append(("fault_put", at))
            else:
                rig.fail_record_once()
                history.append(("fault_record",))
        try:
            if action == "launch":
                if rng.random() < 0.3:
                    rig.sql("UPDATE campaigns SET status='scheduled' WHERE id=?", (cid,))
                    try:
                        result = rig.asyncio.run(rig.launch.launch_campaign(cid, actor="system:scheduler"))
                        history.append(("scheduler", result))
                    except Exception as exc:
                        history.append(("scheduler", type(exc).__name__, str(exc)))
                        assert isinstance(exc, rig.launch.LaunchError) or (
                            fault_kind == "record" and type(exc) is sqlite3.OperationalError
                        ), _message("a", f"unexpected scheduler exception {type(exc).__name__}")
                else:
                    _response(rig.c.post(base + "/launch"))
            elif action == "approve":
                rig.sql("UPDATE campaign_batches SET status='drained', approve_unlock_at=NULL "
                        "WHERE campaign_id=? AND batch_no=? AND status='sending'", (cid, pending - 1))
                _response(rig.c.post(base + f"/batches/{pending}/approve"))
            elif action == "relaunch":
                _press({"expected_seq": _state()[2]})
            elif action == "retry_then_launch":
                rig.fail_pack(rng.choice(live_recorded)["pack_id"])
                _response(rig.c.post(base + "/retry"))
                _response(rig.c.post(base + "/launch"))
            elif action == "resume_then_launch":
                _response(rig.c.post(base + "/pause"))
                _response(rig.c.post(base + "/resume"))
                _response(rig.c.post(base + "/launch"))
            elif action in ("cancel_up", "cancel_down"):
                with rig.monkeypatch.context() as m:
                    if action == "cancel_up":
                        m.setenv("CLOUD_URL", "http://cloud.test")
                    else:
                        m.delenv("CLOUD_URL", raising=False)
                    _response(rig.c.post(base + "/cancel"))
            elif action == "stage":
                stage_number += 1
                tid, sid = rig.stage(cid, stage_number)
                try:
                    result = rig.asyncio.run(rig.launch.launch_campaign(
                        cid, actor="system:scheduler", template_id_override=tid, stage_id=sid))
                    history.append(("stage", result))
                except Exception as exc:
                    rig.sql("UPDATE campaign_stages SET status='failed' WHERE id=?", (sid,))
                    history.append(("stage", type(exc).__name__, str(exc)))
                    assert isinstance(exc, rig.launch.LaunchError) or (
                        fault_kind == "record" and type(exc) is sqlite3.OperationalError
                    ), _message("a", f"unexpected stage exception {type(exc).__name__}")
            elif action == "suppress":
                contact, email = rng.choice(unsuppressed)
                h = rig.hashlib.sha256(email.lower().encode()).hexdigest()
                rig.sql("INSERT INTO suppression_hashes(email_hash, reason, scope) VALUES (?, ?, 'all')",
                        (h, rng.choice(("manual", "unsubscribe"))))
                suppression_snap[contact] = (h, _ids())
            elif action == "outcome_fail":
                rig.fail_pack(rng.choice(live_recorded)["pack_id"])
            elif rng.choice(("overlap", "stale")) == "overlap":
                pre, gen, push = model.unfinished, _state()[1], rig.launch._push_packs
                with rig.monkeypatch.context() as m:
                    orig = rig.launch._ensure_k_temp
                    async def _yield_k_temp(b):
                        await rig.asyncio.sleep(0)
                        return await orig(b)
                    m.setattr(rig.launch, "_ensure_k_temp", _yield_k_temp)
                    async def _both():
                        arrived, barrier = 0, rig.asyncio.Event()
                        async def _push(packs, bearer):
                            nonlocal arrived
                            arrived += 1
                            if arrived == 2:
                                barrier.set()
                            await rig.asyncio.wait_for(barrier.wait(), 2)
                            return await push(packs, bearer)
                        m.setattr(rig.launch, "_push_packs", _push)
                        return await rig.asyncio.gather(
                            rig.launch.launch_campaign(cid, actor="operator:relaunch", relaunch=True),
                            rig.launch.launch_campaign(cid, actor="operator:relaunch", relaunch=True), return_exceptions=True)
                    results = rig.asyncio.run(_both())
                for result in results:
                    # The record fault surfaces directly; a put fault becomes LaunchError.
                    assert isinstance(result, (dict, rig.IntegrityError, rig.launch.LaunchError)) or (
                        fault_kind == "record" and type(result) is sqlite3.OperationalError
                    ), _message("a", repr(result))
                history.append(("double_click", [type(r).__name__ for r in results]))
                _finish_rule(pre, gen)
                model.unfinished = _unfinished()
            else:
                s0 = _state()[2]
                first = _press({"expected_seq": s0})
                s1, after_first = _state()[2], _ids()
                second = _press({"expected_seq": s0})
                if s1 != s0:
                    assert second.status_code == 409, _message("d", "stale second press was not refused")
                    assert second.json()["detail"] == rig.launch._STALE_RELAUNCH, _message("d", "stale detail changed")
                    assert _ids() == after_first, _message("d", "stale second press pushed packs")
                else:
                    assert second.status_code == first.status_code, _message("d", "pre-claim refusal changed")
        finally:
            rig.clear_faults()
        if action != "cancel_down":
            model.unfinished = _unfinished()
        _keys(before)
        snap_after = _state()
        if action == "cancel_down":
            assert snap_after == snap_before, _message("f", "unconfirmed cancel changed markers or generation")
        elif action == "cancel_up":
            assert snap_after[0] == 0 and all(marker is None for _, marker in snap_after[3]), _message("f", "confirmed cancel kept markers")
            assert snap_after[1] == snap_before[1], _message("f", "cancel changed generation")
        _live()
        for h, ids in suppression_snap.values():
            assert all(p["pack_id"] in ids for p in rig.cloud_packs(cid) if p["recipient_hash"] == h), _message("b", "new pack for suppressed contact")
        _settle_fork()


def test_walk_send_paths(rig):
    for seed in FIXED_SEEDS:
        _walk_send_paths(rig, seed, 14)


def test_walk_send_paths_long(rig, request):
    import os

    if "walk_long" not in request.config.option.keyword:
        pytest.skip("long walk: select with -k walk_long")
    start = int(os.environ.get("WALK_SEED_START", "0"))
    count = int(os.environ.get("WALK_SEEDS", "200"))
    print(f"walk window start={start} count={count}", flush=True)
    for seed in range(start, start + count):
        _walk_send_paths(rig, seed, 30)
        print(f"walk seed {seed} ok", flush=True)


# Pytest includes test-function attributes in -k matching; keep the planned test name.
test_walk_send_paths_long.walk_long = True


def test_pack_id_collision_fails_loudly(rig):
    import threading

    cid = rig.campaign(rig.c, 3, "collision")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    packs = rig.cloud_packs(cid)
    pid = packs[0]["pack_id"]
    rig.monkeypatch.setattr(rig.launch, "_pack_id", lambda *args: pid)
    responses = []
    def _press():
        responses.append(rig.c.post(f"/admin/campaigns/{cid}/relaunch"))
    t = threading.Thread(target=_press, daemon=True)
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "collision loop hung: the cap is missing"
    r = responses[0]
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail.startswith(f"campaign {cid}: pack ids for contact ") and "kept colliding after" in detail
    assert len(rig.cloud_packs(cid)) == len(packs)


def test_walk_500_needs_a_fired_fault(rig):
    cid = rig.campaign(rig.c, 3, "fired")
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    rig.fault_put(99)
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 200
    assert rig.faults_fired() == 0
    assert _walk_500_allowed(500, rig.faults_fired()) is False
    rig.clear_faults()
    rig.fault_put(1)
    assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code in (400, 500)
    assert rig.faults_fired() == 1
    assert _walk_500_allowed(500, rig.faults_fired()) is True
    rig.clear_faults()
    assert rig.faults_fired() == 0


def test_finish_batch_all_suppressed_says_so(rig):
    cid = rig.campaign(rig.c, 3, "suppressed")
    rig.fault_put(1)
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code != 200
    assert rig.sql("SELECT dispatch_started_at IS NOT NULL FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=1", (cid,)) == [(1,)]
    assert rig.cloud_packs(cid) == []
    assert rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,)) == []
    rig.clear_faults()
    queued = rig.sql("SELECT c.email FROM campaign_recipients cr JOIN contacts c ON c.id=cr.contact_id "
                     "WHERE cr.campaign_id=? AND cr.batch_no=1 AND cr.status='queued'", (cid,))
    for email, in queued:
        h = rig.hashlib.sha256(email.lower().encode()).hexdigest()
        rig.sql("INSERT INTO suppression_hashes(email_hash, reason, scope) VALUES (?, 'manual', 'all')", (h,))
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200
    assert r.json()["mode"] == "finish-batch"
    assert r.json()["note"].startswith(rig.launch._NOTE_FINISH_BATCH_SUPPRESSED.format(batch_no=1))
    assert "already been handed over" not in r.json()["note"]
    assert rig.cloud_packs(cid) == []


def test_finish_batch_mixed_held_and_suppressed_note(rig):
    rig.settings.set("send_batch_size", 2)
    cid = rig.campaign(rig.c, 2, "mixedsuppressed")
    assert rig.launch.PACK_PUSH_CHUNK == 2
    assert rig.launch._active_seed_emails() == []
    contacts = rig.sql("SELECT c.id, c.email FROM campaign_recipients cr JOIN contacts c ON c.id=cr.contact_id "
                       "WHERE cr.campaign_id=? ORDER BY c.id", (cid,))
    (a, _), (c, email_c) = contacts
    created = rig.sql("SELECT created_at FROM campaigns WHERE id=?", (cid,))[0][0]
    a_pid, c_pid = [rig.launch._pack_id(cid, created, "main", contact, 0) for contact in (a, c)]
    rig.fault_put(2)
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code != 200
    assert rig.sql("SELECT contact_id FROM campaign_recipients WHERE campaign_id=? AND batch_no=1 "
                   "ORDER BY contact_id", (cid,)) == [(a,), (c,)]
    assert rig.sql("SELECT dispatch_started_at IS NOT NULL FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=1", (cid,)) == [(1,)]
    contact_packs = lambda: {p["pack_id"] for p in rig.cloud_packs(cid) if p["pack_id"] in (a_pid, c_pid)}
    assert contact_packs() == {a_pid}
    assert rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=?", (cid,)) == []
    rig.clear_faults()
    h = rig.hashlib.sha256(email_c.lower().encode()).hexdigest()
    rig.sql("INSERT INTO suppression_hashes(email_hash, reason, scope) VALUES (?, 'manual', 'all')", (h,))
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200
    assert r.json()["mode"] == "finish-batch"
    note = r.json()["note"]
    assert not note.startswith(rig.launch._NOTE_FINISH_BATCH_SUPPRESSED.format(batch_no=1))
    assert "on the suppression list" not in note
    assert note.startswith(rig.launch._NOTE_FINISH_BATCH_EMPTY.format(batch_no=1))
    assert rig.sql("SELECT job_id, recipient_id FROM dispatched_job WHERE campaign_id=?", (cid,)) == [(a_pid, a)]
    assert contact_packs() == {a_pid}
    assert r.json()["cloud_ack"]["accepted"] == 0


def test_finish_batch_missing_company_key_is_400(rig):
    from mailman.terminal import routes_admin

    cid = rig.campaign(rig.c, 3, "missingkey")
    rig.fault_put(1)
    assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code != 200
    assert rig.sql("SELECT dispatch_started_at IS NOT NULL FROM campaign_batches "
                   "WHERE campaign_id=? AND batch_no=1", (cid,)) == [(1,)]
    rig.clear_faults()
    packs = rig.cloud_packs(cid)
    message = "company 'X' has no Resend API key configured"
    def _missing_key(entity_id):
        raise ValueError(message)
    rig.monkeypatch.setattr(routes_admin, "get_entity_credentials", _missing_key)
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 400
    assert r.json()["detail"] == message
    assert rig.cloud_packs(cid) == packs


@pytest.mark.parametrize("branch", ["finish-batch", "finish-relaunch", "resend"])
def test_refused_relaunch_changes_only_seq(rig, branch):
    rig.settings.set("send_batch_size", 5)
    cid = rig.campaign(rig.c, 5, f"refused-{branch}")
    assert rig.launch._active_seed_emails() == []
    if branch == "finish-batch":
        rig.fault_put(1)
        assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code != 200
        assert rig.sql("SELECT dispatch_started_at IS NOT NULL FROM campaign_batches "
                       "WHERE campaign_id=? AND batch_no=1", (cid,)) == [(1,)]
        rig.clear_faults()
    else:
        assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
        rig.one_each(cid)
        if branch == "finish-relaunch":
            rig.fault_put(4)
            assert rig.c.post(f"/admin/campaigns/{cid}/relaunch").status_code == 400
            assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(1,)]
            rig.clear_faults()
        else:
            assert rig.sql("SELECT relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(0,)]
        assert rig.sql("SELECT dispatch_started_at FROM campaign_batches WHERE campaign_id=?", (cid,)) == [(None,)]

    contact, original_name = rig.sql(
        "SELECT c.id, c.name FROM campaign_recipients cr JOIN contacts c ON c.id=cr.contact_id "
        "WHERE cr.campaign_id=? ORDER BY c.id DESC LIMIT 1", (cid,))[0]
    if branch == "finish-batch":
        assert rig.sql("SELECT status FROM campaign_recipients WHERE campaign_id=? AND contact_id=?",
                       (cid, contact)) == [("queued",)]
    elif branch == "finish-relaunch":
        created, gen = rig.sql("SELECT created_at, relaunch_gen FROM campaigns WHERE id=?", (cid,))[0]
        pid = rig.launch._pack_id(cid, created, f"relaunch:{gen}", contact, 0)
        assert rig.sql("SELECT job_id FROM dispatched_job WHERE campaign_id=? AND job_id=?", (cid, pid)) == []
    rig.sql("UPDATE contacts SET name=? WHERE id=?", ("Bob {{x}}", contact))

    def _snapshot():
        columns = [r[1] for r in rig.sql("PRAGMA table_info(campaigns)")]
        campaign = dict(zip(columns, rig.sql("SELECT * FROM campaigns WHERE id=?", (cid,))[0]))
        return {"campaign": campaign,
                "batches": rig.sql("SELECT * FROM campaign_batches WHERE campaign_id=? ORDER BY batch_no", (cid,)),
                "recipients": rig.sql("SELECT * FROM campaign_recipients WHERE campaign_id=? ORDER BY contact_id", (cid,)),
                "jobs": rig.sql("SELECT * FROM dispatched_job WHERE campaign_id=? ORDER BY job_id", (cid,)),
                "packs": sorted(rig.cloud_packs(cid), key=lambda p: p["pack_id"])}

    before = _snapshot()
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("1 contact(s) cannot be sent - "), r.text
    after = _snapshot()
    expected_campaign = dict(before["campaign"])
    expected_campaign["relaunch_seq"] += 1
    assert after["campaign"] == expected_campaign
    assert {k: v for k, v in after.items() if k != "campaign"} == {
        k: v for k, v in before.items() if k != "campaign"}

    rig.sql("UPDATE contacts SET name=? WHERE id=?", (original_name, contact))
    r = rig.c.post(f"/admin/campaigns/{cid}/relaunch")
    assert r.status_code == 200, r.text
    gen = before["campaign"]["relaunch_gen"]
    if branch == "finish-batch":
        assert r.json()["mode"] == "finish-batch"
        assert rig.sql("SELECT relaunch_gen FROM campaigns WHERE id=?", (cid,)) == [(gen,)]
        assert rig.sql("SELECT dispatch_started_at FROM campaign_batches WHERE campaign_id=?", (cid,)) == [(None,)]
    elif branch == "finish-relaunch":
        assert r.json()["mode"] == "finish-relaunch"
        assert r.json()["generation"] == gen
        assert rig.sql("SELECT relaunch_gen, relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(gen, 0)]
    else:
        assert r.json()["mode"] == "relaunch"
        assert r.json()["generation"] == gen + 1
        assert rig.sql("SELECT relaunch_gen, relaunch_open FROM campaigns WHERE id=?", (cid,)) == [(gen + 1, 0)]
    assert all(n <= 1 for n in rig.per_gen_live(cid).values())


@pytest.mark.parametrize("route", ["relaunch", "launch", "approve"])
def test_concurrent_press_is_409_without_sql(rig, route, caplog):
    from sqlalchemy.exc import IntegrityError

    if route == "approve":
        rig.settings.set("send_batch_size", 3)
    cid = rig.campaign(rig.c, 6 if route == "approve" else 3, f"concurrent{route}")
    if route != "launch":
        assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    if route == "approve":
        rig.sql("UPDATE campaign_batches SET status='drained', approve_unlock_at=NULL "
                "WHERE campaign_id=? AND batch_no=1", (cid,))
    def _conflict(*args, **kw):
        raise IntegrityError("INSERT INTO dispatched_job (job_id, recipient_hash, content_hash) VALUES (?, ?, ?)",
                             {"recipient_hash": "deadbeef", "content_hash": "cafef00d"},
                             Exception("UNIQUE constraint failed: dispatched_job.job_id"))
    rig.monkeypatch.setattr(rig.launch, "_record_dispatch", _conflict)
    path = f"/admin/campaigns/{cid}/batches/2/approve" if route == "approve" else f"/admin/campaigns/{cid}/{route}"
    with caplog.at_level("WARNING", logger="terminal.launch"):
        r = rig.c.post(path)
    assert r.status_code == 409
    detail = r.json()["detail"]
    m = re.fullmatch(re.escape(rig.launch._CONCURRENT_PRESS) + r" \(ref ([0-9a-f]{8})\)", detail)
    assert m, detail
    logged = [rec for rec in caplog.records if rec.name == "terminal.launch" and rec.levelname == "WARNING"
              and m.group(1) in rec.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info is not None
    assert logged[0].getMessage().startswith(f"{route}:")
    if route == "approve":
        assert rig.sql("SELECT approved_at IS NULL FROM campaign_batches WHERE campaign_id=? AND batch_no=2", (cid,)) == [(1,)]
        assert rig.sql("SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=1", (cid,)) != [("done",)]
    assert all(fragment not in r.text for fragment in (
        "INSERT", "SELECT", "dispatched_job", "[SQL", "recipient_hash", "content_hash", "deadbeef", "cafef00d", "UNIQUE"))


@pytest.mark.parametrize("route", ["relaunch", "launch"])
def test_database_error_is_500_without_sql(rig, route):
    from sqlalchemy.exc import OperationalError

    cid = rig.campaign(rig.c, 3, f"database{route}")
    if route == "relaunch":
        assert rig.c.post(f"/admin/campaigns/{cid}/launch").status_code == 200
    def _database_error(*args, **kw):
        raise OperationalError("INSERT INTO dispatched_job (job_id, recipient_hash, content_hash) VALUES (?, ?, ?)",
                               {"recipient_hash": "deadbeef", "content_hash": "cafef00d"},
                               Exception("UNIQUE constraint failed: dispatched_job.job_id"))
    rig.monkeypatch.setattr(rig.launch, "_record_dispatch", _database_error)
    r = rig.c.post(f"/admin/campaigns/{cid}/{route}")
    assert r.status_code == 500
    assert r.json()["detail"] == f"{route} failed: a database error; the details are in the terminal log"
    assert all(fragment not in r.text for fragment in (
        "INSERT", "SELECT", "dispatched_job", "[SQL", "recipient_hash", "content_hash", "deadbeef", "cafef00d", "UNIQUE"))


def test_walk_refuses_concurrent_press_on_sequential_calls(rig):
    press_text = rig.launch._CONCURRENT_PRESS
    assert _walk_sequential_ok(409, press_text + " (ref 0123abcd)", press_text) is False
    assert _walk_sequential_ok(409, rig.launch._STALE_RELAUNCH, press_text) is True
    assert _walk_sequential_ok(200, None, press_text) is True
    assert _walk_sequential_ok(500, None, press_text) is True
