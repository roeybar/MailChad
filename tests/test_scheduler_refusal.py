"""Permanent scheduler refusals stop once; transient failures retry with bounded audit."""
from __future__ import annotations
import asyncio, html, json, random, sqlite3
from datetime import datetime, timedelta, timezone
import httpx
import pytest

UNFILLED_VARS_ERROR = "unfilled template variables: {{previewText}} - use only the variables listed on the Templates page"
BEARER_ERROR = "cloud bearer missing - handshake helper: " + chr(96) + "scripts/v3 init-handshake" + chr(96) + " (see Known limitations in the README)"


BASE_T = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    from mailman.terminal import sync_client
    offset = timedelta()

    def at(**td):
        nonlocal offset
        offset = timedelta(**td)

    monkeypatch.setattr(sync_client, "_now", lambda: BASE_T + offset)
    return at


def _S(**td):
    return (BASE_T + timedelta(**td)).strftime("%Y-%m-%d %H:%M:%S")


def _age(tmp, cid, delta):
    _sql(tmp, "UPDATE audit_event SET occurred_at=datetime('now', ?) "
              "WHERE target=? AND action='campaign.scheduled_launch_retrying'", (delta, f"campaign:{cid}"))

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


def _drift(tmp, job_id):
    return _sql(tmp, "SELECT severity, category, details_json FROM drift_report WHERE job_id=? ORDER BY id", (job_id,))


def _status(tmp, cid):
    return _sql(tmp, "SELECT status FROM campaigns WHERE id=?", (cid,))[0][0]


def _stage(c, cid, body="<p>{{previewText}}</p>"):
    tid = _template(c, f"stage-{cid}", body)
    return _post(c, f"/admin/campaigns/{cid}/stages",
                 {"template_id": tid, "scheduled_for": "2026-01-01 09:00:00"})["id"]


def _stage_status(tmp, sid):
    return _sql(tmp, "SELECT status FROM campaign_stages WHERE id=?", (sid,))[0][0]


def _page(c, path):
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def test_scheduled_unfilled_variables_refusal_fails_once_and_shows_reason(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "unfilled", "<p>{{previewText}}</p>")
    _schedule(tmp, cid)
    tick(3)
    assert _status(tmp, cid) == "failed"
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [{"error": UNFILLED_VARS_ERROR, "marked_failed": True}]
    drift = _drift(tmp, f"sched:{cid}")
    assert len(drift) == 1
    assert drift[0][:2] == ("WARN", "status_mismatch")
    assert json.loads(drift[0][2]) == {"campaign_id": cid, "error": UNFILLED_VARS_ERROR, "marked_failed": True}
    assert _audit(tmp, cid, "campaign.scheduled_launch_retrying") == []
    assert packs == []
    assert _sql(tmp, "SELECT lock_until FROM campaigns WHERE id=?", (cid,)) == [(None,)]
    assert _sql(tmp, "SELECT COUNT(*) FROM campaign_batches WHERE campaign_id=?", (cid,)) == [(0,)]
    page = _page(c, f"/admin/campaigns/{cid}/analytics")
    assert 'callout crit' in page
    assert "The scheduler did not launch this campaign." in page
    assert html.escape(UNFILLED_VARS_ERROR) + '.<div' in page
    failed_pill = '<span class="pill crit" style="margin-left:4px"><span class="dot"></span>failed</span>'
    assert failed_pill in page
    assert failed_pill in _page(c, f"/admin/campaigns/{cid}/recipients")
    assert "has not launched this campaign yet" not in page
    assert '<span class="pill crit"><span class="dot"></span>failed</span>' in _page(c, "/admin/campaigns")


def test_refused_campaign_does_not_starve_others(rig):
    c, ids, tmp, packs, k = rig
    a = _mk(c, "bad-first", "<p>{{previewText}}</p>")
    b = _mk(c, "good-second", "<p>Hello {{name}}</p>")
    _schedule(tmp, a)
    _schedule(tmp, b)
    tick()
    assert _status(tmp, a) == "failed"
    assert _status(tmp, b) == "dispatched"
    assert len(packs) == 2
    expected = _sql(tmp, "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id=?", (b,))[0][0]
    assert len({p["recipient_hash"] for p in packs}) == expected


def test_scheduled_untested_campaign_is_refused(rig):
    c, ids, tmp, packs, k = rig
    tid = _template(c, "untested", "<p>Hello</p>")
    cid = _post(c, "/admin/campaigns", {"name": "Untested", "template_id": tid, "entity_id": ids["eid"],
                                       "scheduled_for": "2026-01-01 09:00:00"})["id"]
    tick(2)
    assert _status(tmp, cid) == "failed"
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [
        {"error": "test send required before launch", "marked_failed": True}]


def test_transient_failure_retries_next_tick(rig, monkeypatch):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "transient", "<p>Hello</p>")
    _schedule(tmp, cid)
    attempts = []

    async def _flaky(bearer):
        attempts.append(bearer)
        if len(attempts) <= 2:
            raise httpx.ConnectError("cloud unreachable (test)")
        return k

    monkeypatch.setattr(launch, "_ensure_k_temp", _flaky)
    tick(2)
    assert _status(tmp, cid) == "scheduled"
    assert [r["error"] for r in _audit(tmp, cid, "campaign.scheduled_launch_retrying")] == ["cloud unreachable (test)"]
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == []
    assert _drift(tmp, f"sched:{cid}") == []
    tick()
    assert _status(tmp, cid) == "dispatched"
    assert len(_audit(tmp, cid, "campaign.launched")) == 1


def test_missing_bearer_keeps_campaign_scheduled(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "bearer", "<p>Hello</p>")
    _schedule(tmp, cid)
    bearer = tmp / "keys" / "cloud_bearer.txt"
    bearer.unlink()
    tick(2)
    assert _status(tmp, cid) == "scheduled"
    rows = _audit(tmp, cid, "campaign.scheduled_launch_retrying")
    assert len(rows) == 1 and rows[0]["error"].startswith("cloud bearer missing")
    bearer.write_text("tok")
    tick()
    assert _status(tmp, cid) == "dispatched"


def test_lock_conflict_keeps_campaign_scheduled(rig):
    c, ids, tmp, packs, k = rig
    x = _mk(c, "locked", "<p>Hello</p>")
    tid = _template(c, "lock-y", "<p>Hello</p>")
    y = _post(c, "/admin/campaigns", {"name": "Y", "template_id": tid, "entity_id": ids["eid"]})["id"]
    _post(c, f"/admin/campaigns/{y}/recipients", {"by_tag": "locked"})
    from mailman.terminal import routes_admin
    routes_admin.mark_tested(y)
    _sql(tmp, "UPDATE campaigns SET status='dispatched', dispatched_at=datetime('now'), "
              "lock_until=datetime('now','+1 day') WHERE id=?", (x,))
    _schedule(tmp, y)
    tick()
    assert _status(tmp, y) == "scheduled"
    rows = _audit(tmp, y, "campaign.scheduled_launch_retrying")
    assert len(rows) == 1 and "locked by another running campaign" in rows[0]["error"]
    _sql(tmp, "UPDATE campaigns SET lock_until=datetime('now','-1 minute') WHERE id=?", (x,))
    tick()
    assert _status(tmp, y) == "dispatched"


def test_post_commit_push_error_recorded_once(rig, monkeypatch):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "postcommit", "<p>Hello</p>")
    _schedule(tmp, cid)

    async def _broken_push(p, bearer):
        raise httpx.ConnectError("post-commit push failed (test)")

    monkeypatch.setattr(launch, "_push_packs", _broken_push)
    tick()
    assert _status(tmp, cid) == "dispatched"
    assert _sql(tmp, "SELECT batch_no, status FROM campaign_batches WHERE campaign_id=?", (cid,)) == [(1, "pending")]
    details = [{"error": "post-commit push failed (test)", "marked_failed": False}]
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == details
    drift = _drift(tmp, f"sched:{cid}")
    assert len(drift) == 1
    assert json.loads(drift[0][2]) == dict(details[0], campaign_id=cid)
    assert _audit(tmp, cid, "campaign.scheduled_launch_retrying") == []
    tick()
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == details
    assert _drift(tmp, f"sched:{cid}") == drift


@pytest.mark.parametrize("change", ["pause", "reschedule"])
def test_refusal_never_overrides_operator_change(rig, monkeypatch, change):
    from mailman.terminal import launch, routes_admin
    c, ids, tmp, packs, k = rig
    cid = _mk(c, f"race-{change}", "<p>{{previewText}}</p>")
    _schedule(tmp, cid)
    real = launch.launch_campaign

    async def _operator_change(campaign_id, **kwargs):
        if change == "pause":
            routes_admin.pause_campaign(campaign_id)
        else:
            _sql(tmp, "UPDATE campaigns SET scheduled_for='2999-01-01 00:00:00' WHERE id=?", (campaign_id,))
        return await real(campaign_id, **kwargs)

    monkeypatch.setattr(launch, "launch_campaign", _operator_change)
    tick()
    assert _status(tmp, cid) == ("paused" if change == "pause" else "scheduled")
    rows = _audit(tmp, cid, "campaign.scheduled_launch_failed")
    assert len(rows) == 1 and rows[0]["marked_failed"] is False
    tick()
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == rows


def test_scheduled_stage_refused_once_with_reason(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-bad", "<p>Hello</p>")
    sid = _stage(c, cid)
    tick(3)
    assert _stage_status(tmp, sid) == "failed"
    assert _audit(tmp, cid, "campaign.stage_failed") == [{"stage_id": sid, "error": UNFILLED_VARS_ERROR, "possibly_sent": False}]
    drift = _drift(tmp, f"stage:{sid}")
    assert len(drift) == 1
    assert json.loads(drift[0][2]) == {"campaign_id": cid, "stage_id": sid, "error": UNFILLED_VARS_ERROR, "possibly_sent": False}
    assert _status(tmp, cid) == "tested"
    assert packs == []
    page = _page(c, f"/admin/campaigns/{cid}/recipients")
    assert 'callout crit' in page and html.escape(UNFILLED_VARS_ERROR) in page
    assert "Not sent: " + html.escape(UNFILLED_VARS_ERROR) + ". Delete this stage" in page
    assert "may already be on its way" not in page


def test_stage_missing_bearer_stays_pending(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-bearer", "<p>Hello</p>")
    sid = _stage(c, cid, "<p>Hello</p>")
    bearer = tmp / "keys" / "cloud_bearer.txt"
    bearer.unlink()
    tick(2)
    assert _stage_status(tmp, sid) == "pending"
    assert _audit(tmp, cid, "campaign.stage_retrying") == [{"stage_id": sid, "error": BEARER_ERROR}]
    bearer.write_text("tok")
    tick()
    assert _stage_status(tmp, sid) == "dispatched"


def test_stage_unknown_error_still_fails(rig, monkeypatch):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-unknown", "<p>Hello</p>")
    sid = _stage(c, cid, "<p>Hello</p>")

    async def _unknown(bearer):
        raise httpx.ConnectError("unknown stage error (test)")

    monkeypatch.setattr(launch, "_ensure_k_temp", _unknown)
    tick()
    assert _stage_status(tmp, sid) == "failed"
    assert _audit(tmp, cid, "campaign.stage_failed") == [
        {"stage_id": sid, "error": "unknown stage error (test)", "possibly_sent": True}]
    drift = _drift(tmp, f"stage:{sid}")
    assert len(drift) == 1
    assert json.loads(drift[0][2]) == {
        "campaign_id": cid, "stage_id": sid, "error": "unknown stage error (test)", "possibly_sent": True}


def test_retryable_maps_to_http_400(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "http", "<p>Hello</p>")
    (tmp / "keys" / "cloud_bearer.txt").unlink()
    r = c.post(f"/admin/campaigns/{cid}/launch")
    assert r.status_code == 400
    assert r.json()["detail"] == BEARER_ERROR


def test_refusal_reason_is_escaped(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "escape", "<p>Hi {{name}}</p>")
    _sql(tmp, "UPDATE contacts SET name=? WHERE email=?", ("Eve {{<i>x</i>}}", "escape0@demo.example"))
    _schedule(tmp, cid)
    tick()
    assert _status(tmp, cid) == "failed"
    page = _page(c, f"/admin/campaigns/{cid}/analytics")
    assert html.escape("{{<i>x</i>}}") in page
    assert "<i>x</i>" not in page


def test_handbook_scheduled_text(rig):
    c, ids, tmp, packs, k = rig
    page = _page(c, "/admin/docs")
    assert "moves to <code>failed</code>" in page
    assert ("If unexpected errors keep it from launching for a day, it moves to <code>failed</code>. "
            "Time the terminal is offline counts toward that day.") in page
    assert "remains in <code>scheduled</code> status for retry" not in page


def test_stress_many_due_campaigns_many_ticks(rig, monkeypatch):
    """Sequential volume test; the scheduler is single-threaded."""
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    rng = random.Random(7)
    cases, failures = {}, {}
    kinds = ["unfilled", "clean", "flaky"]
    for i in range(24):
        kind = kinds[i] if i < 3 else rng.choice(kinds)
        n = rng.randint(1, 3)
        cid = _mk(c, f"stress-{i}", "<p>{{previewText}}</p>" if kind == "unfilled" else "<p>Hello {{name}}</p>", n)
        _schedule(tmp, cid)
        cases[cid] = (kind, n)
        if kind == "flaky":
            failures[cid] = 3 if i == 2 else rng.randint(1, 3)
    real = launch.launch_campaign
    current = None

    async def _track(campaign_id, **kwargs):
        nonlocal current
        current = campaign_id
        return await real(campaign_id, **kwargs)

    async def _flaky(bearer):
        if failures.get(current, 0):
            failures[current] -= 1
            raise httpx.ConnectError("cloud unreachable (stress)")
        return k

    monkeypatch.setattr(launch, "launch_campaign", _track)
    monkeypatch.setattr(launch, "_ensure_k_temp", _flaky)
    tick(5)
    expected_packs, unfilled_count = 0, 0
    for cid, (kind, n) in cases.items():
        if kind == "unfilled":
            unfilled_count += 1
            assert _status(tmp, cid) == "failed", cid
            assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [{"error": UNFILLED_VARS_ERROR, "marked_failed": True}]
            assert len(_drift(tmp, f"sched:{cid}")) == 1
        else:
            expected_packs += n
            assert _status(tmp, cid) == "dispatched", cid
            assert len(_audit(tmp, cid, "campaign.launched")) == 1
            assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == []
            if kind == "flaky":
                assert [r["error"] for r in _audit(tmp, cid, "campaign.scheduled_launch_retrying")] == ["cloud unreachable (stress)"]
    assert _sql(tmp, "SELECT COUNT(*) FROM drift_report") == [(unfilled_count,)]
    hashes = [p["recipient_hash"] for p in packs]
    assert len(hashes) == len(set(hashes)) == expected_packs


@pytest.mark.parametrize("chunk", [1, 2])
def test_stage_push_rejection_is_possibly_sent(rig, monkeypatch, chunk):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, f"push-{chunk}", "<p>Hello</p>", n=2)
    sid = _stage(c, cid, "<p>Hello</p>")
    real_client = httpx.AsyncClient
    seen = []

    def h(request):
        seen.append(request)
        if len(seen) == chunk:
            return httpx.Response(500, text="<b>boom</b>")
        return httpx.Response(200, json={"accepted": 1, "submitted": 1, "enqueued": 0, "deferred": 0})

    monkeypatch.setattr(launch, "_push_packs", ids["real_push"])
    monkeypatch.setattr(launch, "PACK_PUSH_CHUNK", 1)
    monkeypatch.setattr(launch.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(h), **kw))
    tick()
    err = f"cloud /packs rejected (500) on chunk {chunk}: <b>boom</b>"
    assert _stage_status(tmp, sid) == "failed"
    assert _audit(tmp, cid, "campaign.stage_failed") == [
        {"stage_id": sid, "error": err, "possibly_sent": True}]
    drift = _drift(tmp, f"stage:{sid}")
    assert len(drift) == 1
    assert json.loads(drift[0][2]) == {
        "campaign_id": cid, "stage_id": sid, "error": err, "possibly_sent": True}
    assert _sql(tmp, "SELECT COUNT(*) FROM dispatched_job WHERE campaign_id=?", (cid,)) == [(chunk - 1,)]
    page = _page(c, f"/admin/campaigns/{cid}/recipients")
    assert "Stage failed: " + html.escape(err) + ". Some of its mail may already be on its way" in page
    assert "<b>boom</b>" not in page
    assert "Not sent:" not in page


def test_stage_reason_is_escaped(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-escape", "<p>Hello</p>")
    sid = _stage(c, cid, "<p>Hi {{name}}</p>")
    _sql(tmp, "UPDATE contacts SET name=? WHERE email=?", ("Eve {{<i>x</i>}}", "stage-escape0@demo.example"))
    tick()
    assert _stage_status(tmp, sid) == "failed"
    rows = _audit(tmp, cid, "campaign.stage_failed")
    assert len(rows) == 1 and rows[0]["possibly_sent"] is False
    page = _page(c, f"/admin/campaigns/{cid}/recipients")
    assert "Not sent: " in page
    assert html.escape("{{<i>x</i>}}") in page
    assert "<i>x</i>" not in page


def test_empty_reason_falls_back_to_type_name(rig, monkeypatch):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    a = _mk(c, "empty-camp", "<p>Hello</p>")
    b = _mk(c, "empty-stage", "<p>Hello</p>")
    _schedule(tmp, a)
    sid = _stage(c, b, "<p>Hello</p>")

    async def broken(bearer):
        raise TimeoutError()

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    assert [r["error"] for r in _audit(tmp, a, "campaign.scheduled_launch_retrying")] == ["TimeoutError"]
    assert "yet: TimeoutError. It tries again every minute." in _page(c, f"/admin/campaigns/{a}/analytics")
    assert _audit(tmp, b, "campaign.stage_failed") == [
        {"stage_id": sid, "error": "TimeoutError", "possibly_sent": True}]
    assert "Stage failed: TimeoutError." in _page(c, f"/admin/campaigns/{b}/recipients")


def test_scheduled_retry_reason_shows_on_analytics(rig, monkeypatch):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "retry-page", "<p>Hello</p>")
    _schedule(tmp, cid)

    async def broken(bearer):
        raise httpx.ConnectError("<b>down</b>")

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    page = _page(c, f"/admin/campaigns/{cid}/analytics")
    assert ("The scheduler has not launched this campaign yet: &lt;b&gt;down&lt;/b&gt;. "
            "It tries again every minute.") in page
    assert "<b>down</b>" not in page
    _sql(tmp, "UPDATE campaigns SET status='failed' WHERE id=?", (cid,))
    assert "has not launched this campaign yet" not in _page(c, f"/admin/campaigns/{cid}/analytics")
    _sql(tmp, "UPDATE campaigns SET status='scheduled', updated_at=datetime('now','+1 minute') WHERE id=?", (cid,))
    assert "has not launched this campaign yet" not in _page(c, f"/admin/campaigns/{cid}/analytics")


def test_alternating_unexpected_errors_refuse_after_a_day(rig, monkeypatch, clock):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "alternating", "<p>Hello</p>")
    _schedule(tmp, cid)
    reason = "A (test)"

    async def broken(bearer):
        raise httpx.ConnectError(reason)

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    assert _status(tmp, cid) == "scheduled"
    reason = "B (test)"
    clock(hours=12)
    tick()
    assert _status(tmp, cid) == "scheduled"
    reason = "A (test)"
    clock(hours=23, minutes=59, seconds=59)
    tick()
    assert _status(tmp, cid) == "scheduled"
    rows = _audit(tmp, cid, "campaign.scheduled_launch_retrying")
    assert [r["error"] for r in rows] == ["A (test)", "B (test)"]
    assert [r["streak_start"] for r in rows] == [_S(), _S()]
    assert "yet: B (test)." in _page(c, f"/admin/campaigns/{cid}/analytics")
    clock(hours=24)
    tick()
    assert _status(tmp, cid) == "failed"
    failed = [{"error": "A (test)", "marked_failed": True}]
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == failed
    drift = _drift(tmp, f"sched:{cid}")
    assert len(drift) == 1
    assert json.loads(drift[0][2]) == dict(failed[0], campaign_id=cid)
    page = _page(c, f"/admin/campaigns/{cid}/analytics")
    assert "callout crit" in page and "A (test)." in page
    assert "has not launched this campaign yet" not in page
    tick()
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == failed
    assert _audit(tmp, cid, "campaign.scheduled_launch_retrying") == rows
    assert _drift(tmp, f"sched:{cid}") == drift


def test_retryable_reason_never_counts(rig, clock):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "retryable-day", "<p>Hello</p>")
    _schedule(tmp, cid)
    since = _sql(tmp, "SELECT updated_at FROM campaigns WHERE id=?", (cid,))[0][0]
    (tmp / "keys" / "cloud_bearer.txt").unlink()
    tick()
    _age(tmp, cid, "-25 hours")
    clock(hours=25)
    tick()
    clock(hours=48)
    tick()
    assert _status(tmp, cid) == "scheduled"
    assert _audit(tmp, cid, "campaign.scheduled_launch_retrying") == [
        {"error": BEARER_ERROR, "updated_at": since}] * 2
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == []


def test_retry_writes_do_not_reset_the_day(rig, monkeypatch, clock):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "writes-day", "<p>Hello</p>")
    _schedule(tmp, cid)
    since = _sql(tmp, "SELECT updated_at FROM campaigns WHERE id=?", (cid,))[0][0]
    reason = "A (test)"

    async def broken(bearer):
        raise httpx.ConnectError(reason)

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    for td, reason in [({}, "A (test)"), ({"hours": 6}, "A (test)"),
                       ({"hours": 12}, "B (test)"), ({"hours": 23, "minutes": 59, "seconds": 59}, "A (test)")]:
        clock(**td)
        tick()
        assert _status(tmp, cid) == "scheduled"
        assert _sql(tmp, "SELECT updated_at FROM campaigns WHERE id=?", (cid,)) == [(since,)]
        _page(c, f"/admin/campaigns/{cid}/analytics")
        assert _sql(tmp, "SELECT updated_at FROM campaigns WHERE id=?", (cid,)) == [(since,)]
    rows = _audit(tmp, cid, "campaign.scheduled_launch_retrying")
    assert [r["error"] for r in rows] == ["A (test)", "B (test)"]
    assert [r["streak_start"] for r in rows] == [_S(), _S()]
    assert [r["updated_at"] for r in rows] == [since, since]
    clock(hours=24)
    tick()
    assert _status(tmp, cid) == "failed"


def test_success_between_failures_resets_the_day(rig, monkeypatch, clock):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "success-reset", "<p>Hello</p>")
    _schedule(tmp, cid)
    fail = True

    async def sometimes(bearer):
        if fail:
            raise httpx.ConnectError("A (test)")
        return k

    monkeypatch.setattr(launch, "_ensure_k_temp", sometimes)
    tick()
    fail = False
    clock(hours=12)
    tick()
    assert _status(tmp, cid) == "dispatched"
    _sql(tmp, "UPDATE campaigns SET status='scheduled' WHERE id=?", (cid,))
    _sql(tmp, "UPDATE campaign_recipients SET status='queued' WHERE campaign_id=?", (cid,))
    fail = True
    clock(hours=25)
    tick()
    assert _status(tmp, cid) == "scheduled"
    assert _audit(tmp, cid, "campaign.scheduled_launch_retrying")[-1]["streak_start"] == _S(hours=25)
    clock(hours=48, minutes=59, seconds=59)
    tick()
    assert _status(tmp, cid) == "scheduled"
    clock(hours=49)
    tick()
    assert _status(tmp, cid) == "failed"


def test_operator_edit_resets_the_day(rig, monkeypatch, clock):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "edit-reset", "<p>Hello</p>")
    _schedule(tmp, cid)

    async def broken(bearer):
        raise httpx.ConnectError("A (test)")

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    clock(hours=20)
    r = c.patch(f"/admin/campaigns/{cid}", json={"name": "Edited"})
    assert r.status_code == 200, r.text
    tick()
    rows = _audit(tmp, cid, "campaign.scheduled_launch_retrying")
    assert [r["streak_start"] for r in rows] == [_S(), _S(hours=20)]
    clock(hours=43, minutes=59, seconds=59)
    tick()
    assert _status(tmp, cid) == "scheduled"
    clock(hours=44)
    tick()
    assert _status(tmp, cid) == "failed"


@pytest.mark.parametrize("kind", ["no_key", "bad_kek"])
def test_real_credentials_refusals(rig, monkeypatch, clock, kind):
    from cryptography.exceptions import InvalidTag
    from mailman.terminal import routes_admin, settings
    c, ids, tmp, packs, k = rig
    cid = _mk(c, f"creds-{kind}", "<p>Hello</p>")
    _schedule(tmp, cid)
    monkeypatch.setattr(routes_admin, "get_entity_credentials", ids["real_creds"])
    if kind == "no_key":
        tick()
        assert _status(tmp, cid) == "failed"
        assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [
            {"error": "company 'Demo Co' has no Resend API key configured", "marked_failed": True}]
    else:
        _sql(tmp, "UPDATE entities SET resend_key_enc='AAAA' WHERE id=?", (ids["eid"],))

        def bad_decrypt(blob):
            raise InvalidTag()

        monkeypatch.setattr(settings, "_decrypt", bad_decrypt)
        tick()
        assert _status(tmp, cid) == "scheduled"
        assert [r["error"] for r in _audit(tmp, cid, "campaign.scheduled_launch_retrying")] == ["InvalidTag"]
        clock(hours=24)
        tick()
        assert _status(tmp, cid) == "failed"
        assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [
            {"error": "InvalidTag", "marked_failed": True}]


def test_stress_streaks_many_campaigns(rig, monkeypatch):
    """Sequential volume test; the scheduler is single-threaded."""
    from mailman.terminal import launch, sync_client
    c, ids, tmp, packs, k = rig
    rng = random.Random(11)
    fixed = [timedelta(hours=2), timedelta(hours=12), timedelta(hours=24, seconds=-1),
             timedelta(hours=24), timedelta(hours=25), timedelta(hours=47)]
    cases, offsets, edited = {}, {}, set()
    for i in range(12):
        cid = _mk(c, f"streak-stress-{i}", "<p>Hello</p>")
        _schedule(tmp, cid)
        cases[cid] = fixed[i] if i < 6 else rng.choice(fixed)
        offsets[cid] = timedelta()
        if i == 4 or rng.random() < 0.3:
            edited.add(cid)
    real = launch.launch_campaign
    current = None

    async def track(campaign_id, **kwargs):
        nonlocal current
        current = campaign_id
        return await real(campaign_id, **kwargs)

    async def broken(bearer):
        raise httpx.ConnectError("cloud unreachable (stress streak)")

    monkeypatch.setattr(launch, "launch_campaign", track)
    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    monkeypatch.setattr(sync_client, "_now", lambda: BASE_T + offsets[current])
    tick()
    for cid, offset in cases.items():
        offsets[cid] = offset
        if cid in edited:
            r = c.patch(f"/admin/campaigns/{cid}", json={"name": f"Edited {cid}"})
            assert r.status_code == 200, r.text
    tick()
    failed_count, snapshot = 0, {}
    for cid, offset in cases.items():
        failed = cid not in edited and offset >= timedelta(hours=24)
        assert _status(tmp, cid) == ("failed" if failed else "scheduled"), cid
        failures = _audit(tmp, cid, "campaign.scheduled_launch_failed")
        drift = _drift(tmp, f"sched:{cid}")
        if failed:
            failed_count += 1
            assert len(failures) == 1 and failures[0]["marked_failed"] is True
            assert len(drift) == 1
        else:
            assert failures == []
            assert drift == []
        rows = _audit(tmp, cid, "campaign.scheduled_launch_retrying")
        if cid in edited:
            assert len(rows) == 2 and rows[0]["streak_start"] != rows[1]["streak_start"]
        else:
            assert len(rows) == 1
        snapshot[cid] = (_status(tmp, cid), failures, drift, rows)
    assert _sql(tmp, "SELECT COUNT(*) FROM drift_report") == [(failed_count,)]
    tick()
    for cid in cases:
        assert (_status(tmp, cid), _audit(tmp, cid, "campaign.scheduled_launch_failed"),
                _drift(tmp, f"sched:{cid}"), _audit(tmp, cid, "campaign.scheduled_launch_retrying")) == snapshot[cid]


def test_retry_callout_drops_one_trailing_period(rig, monkeypatch):
    from mailman.terminal import launch, routes_admin
    c, ids, tmp, packs, k = rig
    x = _mk(c, "period-lock", "<p>Hello</p>")
    tid = _template(c, "period-y", "<p>Hello</p>")
    y = _post(c, "/admin/campaigns", {"name": "Y", "template_id": tid, "entity_id": ids["eid"]})["id"]
    _post(c, f"/admin/campaigns/{y}/recipients", {"by_tag": "period-lock"})
    routes_admin.mark_tested(y)
    _sql(tmp, "UPDATE campaigns SET status='dispatched', dispatched_at=datetime('now'), "
              "lock_until=datetime('now','+1 day') WHERE id=?", (x,))
    _schedule(tmp, y)
    tick()
    page = _page(c, f"/admin/campaigns/{y}/analytics")
    assert "one campaign per day. It tries again every minute." in page
    assert "day.." not in page
    z = _mk(c, "period-wait", "<p>Hello</p>")
    _schedule(tmp, z)

    async def broken(bearer):
        raise httpx.ConnectError("wait..")

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    page = _page(c, f"/admin/campaigns/{z}/analytics")
    assert "yet: wait. It tries" in page
    assert "wait.." not in page


def test_retryable_reason_does_not_reset_the_day(rig, monkeypatch, clock):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "retryable-gap", "<p>Hello</p>")
    _schedule(tmp, cid)

    async def broken(bearer):
        raise httpx.ConnectError("A (test)")

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    bearer = tmp / "keys" / "cloud_bearer.txt"
    bearer.unlink()
    clock(hours=12)
    tick()
    assert _status(tmp, cid) == "scheduled"
    rows = _audit(tmp, cid, "campaign.scheduled_launch_retrying")
    assert [r["error"] for r in rows] == ["A (test)", BEARER_ERROR]
    bearer.write_text("tok")
    clock(hours=24)
    tick()
    assert _status(tmp, cid) == "failed"


@pytest.mark.parametrize("kind", ["not_sent", "possibly_sent"])
def test_stage_callouts_strip_trailing_periods(rig, monkeypatch, kind):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, f"stage-period-{kind}", "<p>Hello</p>")
    sid = _stage(c, cid, "<p>Hello</p>")
    if kind == "not_sent":
        def refused(*args):
            raise launch.LaunchError("bad (test).")

        monkeypatch.setattr(launch, "_preflight", refused)
        text = "Not sent: bad (test). Delete"
    else:
        async def broken(bearer):
            raise httpx.ConnectError("down (test).")

        monkeypatch.setattr(launch, "_ensure_k_temp", broken)
        text = "Stage failed: down (test). Some of its mail"
    tick()
    assert _stage_status(tmp, sid) == "failed"
    page = _page(c, f"/admin/campaigns/{cid}/recipients")
    assert text in page
    assert "(test).." not in page


def test_refusal_callout_ends_with_one_period(rig, monkeypatch):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    for i, reason in enumerate(["refused (test).", "refused (test)"]):
        cid = _mk(c, f"refusal-period-{i}", "<p>Hello</p>")
        _schedule(tmp, cid)

        def refused(*args):
            raise launch.LaunchError(reason)

        monkeypatch.setattr(launch, "_preflight", refused)
        tick()
        assert _status(tmp, cid) == "failed"
        page = _page(c, f"/admin/campaigns/{cid}/analytics")
        assert "did not launch this campaign.</b> refused (test).<div" in page
        assert "(test).." not in page


def test_retryable_callout_survives_operator_edit(rig):
    from mailman.terminal import routes_admin
    c, ids, tmp, packs, k = rig
    x = _mk(c, "edit-lock", "<p>Hello</p>")
    tid = _template(c, "edit-lock-y", "<p>Hello</p>")
    y = _post(c, "/admin/campaigns", {"name": "Y", "template_id": tid, "entity_id": ids["eid"]})["id"]
    _post(c, f"/admin/campaigns/{y}/recipients", {"by_tag": "edit-lock"})
    routes_admin.mark_tested(y)
    _sql(tmp, "UPDATE campaigns SET status='dispatched', dispatched_at=datetime('now'), "
              "lock_until=datetime('now','+1 day') WHERE id=?", (x,))
    _schedule(tmp, y)
    tick()
    _age(tmp, y, "-1 hour")
    r = c.patch(f"/admin/campaigns/{y}", json={"name": "Edited Y"})
    assert r.status_code == 200, r.text
    tick()
    assert _status(tmp, y) == "scheduled"
    rows = _audit(tmp, y, "campaign.scheduled_launch_retrying")
    assert len(rows) == 2
    assert all("locked by another running campaign" in r["error"] for r in rows)
    page = _page(c, f"/admin/campaigns/{y}/analytics")
    assert "has not launched this campaign yet" in page
    assert html.escape(rows[-1]["error"].rstrip(".")) in page


def test_edit_during_refusal_wins(rig, monkeypatch, clock):
    from mailman.terminal import launch, sync_client
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "edit-during-refusal", "<p>Hello</p>")
    _schedule(tmp, cid)

    async def broken(bearer):
        raise httpx.ConnectError("cloud unreachable (test)")

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    monkeypatch.setattr(sync_client, "_streak", lambda campaign_id, now: (BASE_T, "1999-01-01 00:00:00"))
    clock(hours=24)
    tick()
    assert _status(tmp, cid) == "scheduled"
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [
        {"error": "cloud unreachable (test)", "marked_failed": False}]


def test_flagless_stage_failure_counts_as_possibly_sent(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-flagless", "<p>Hello</p>")
    sid = _stage(c, cid)
    tick(3)
    _sql(tmp, "DELETE FROM audit_event WHERE target=? AND action='campaign.stage_failed'", (f"campaign:{cid}",))
    _sql(tmp, "INSERT INTO audit_event (actor, action, target, details_json) "
              "VALUES ('system:scheduler', 'campaign.stage_failed', ?, ?)",
         (f"campaign:{cid}", json.dumps({"stage_id": sid, "error": "cloud /packs rejected (500) on chunk 2: boom"})))
    _sql(tmp, "UPDATE campaign_stages SET status='failed' WHERE id=? AND status!='failed'", (sid,))
    page = _page(c, f"/admin/campaigns/{cid}/recipients")
    assert "Stage failed: cloud /packs rejected (500) on chunk 2: boom. Some of its mail may already be on its way" in page
    assert "Not sent:" not in page
