"""Tested scheduling, local-time forms and scheduler flows."""
from __future__ import annotations
import asyncio, html, json, random, sqlite3
from datetime import datetime, timedelta, timezone
import httpx
import pytest


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


def _audit(tmp, cid, action):
    return [json.loads(r[0]) for r in _sql(
        tmp, "SELECT details_json FROM audit_event WHERE target=? AND action=? ORDER BY id",
        (f"campaign:{cid}", action))]


def _drift(tmp, job_id):
    return _sql(tmp, "SELECT severity, category, details_json FROM drift_report WHERE job_id=? ORDER BY id", (job_id,))


def _status(tmp, cid):
    return _sql(tmp, "SELECT status FROM campaigns WHERE id=?", (cid,))[0][0]


def _page(c, path):
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


FUTURE = "2999-01-01T09:00"
FUTURE_DB = "2999-01-01 09:00:00"


def _due(tmp, cid):
    _sql(tmp, "UPDATE campaigns SET scheduled_for='2026-01-01 09:00:00' WHERE id=?", (cid,))


def _contacts(c, tag, n):
    for i in range(n):
        _post(c, "/admin/contacts", {"email": f"{tag}{i}@demo.example", "name": f"Person {i}",
                                    "tags": tag, "consent_source": "demo"})


def _mk(c, tag, html, n=2, tested=True):
    from sqlalchemy import select
    from mailman.terminal import db
    from mailman.terminal.models import Entity
    _contacts(c, tag, n)
    tid = _template(c, tag, html)
    with db.session() as s:
        eid = s.execute(select(Entity.id).where(Entity.name == "Demo Co")).scalar_one()
    cid = _post(c, "/admin/campaigns", {"name": f"C {tag}", "template_id": tid, "entity_id": eid})["id"]
    _post(c, f"/admin/campaigns/{cid}/recipients", {"by_tag": tag})
    if tested:
        r = c.post(f"/admin/campaigns/{cid}/mark-tested")
        assert r.status_code == 200, r.text
        assert r.json()["tested"] is True
    return cid


def _create_scheduled_ui(c, name, tid, eid):
    from sqlalchemy import select
    from mailman.terminal import db
    from mailman.terminal.models import Campaign
    r = c.post("/admin/campaigns/create", data={"name": name, "template_id": tid,
               "entity_id": eid, "scheduled_for": FUTURE}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with db.session() as s:
        return s.execute(select(Campaign.id).where(Campaign.name == name)).scalar_one()


def _schedule(c, cid, at=FUTURE):
    return c.post(f"/admin/campaigns/{cid}/schedule", json={"scheduled_for": at})


def test_schedule_then_test_then_tick_dispatches(rig):
    c, ids, tmp, packs, k = rig
    tid = _template(c, "schedule-first", "<p>Hello</p>")
    _contacts(c, "schedule-first", 2)
    cid = _create_scheduled_ui(c, "Schedule first", tid, ids["eid"])
    _post(c, f"/admin/campaigns/{cid}/recipients", {"by_tag": "schedule-first"})
    assert _status(tmp, cid) == "scheduled"
    assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(FUTURE_DB,)]
    tick()
    assert _status(tmp, cid) == "scheduled" and packs == []
    r = c.post(f"/admin/campaigns/{cid}/mark-tested")
    assert r.status_code == 200 and r.json()["tested"] is True
    assert _status(tmp, cid) == "scheduled"
    stamp, captured = _sql(tmp, "SELECT test_sent_at, template_hash_at_test FROM campaigns WHERE id=?", (cid,))[0]
    assert stamp
    assert captured == _sql(tmp, "SELECT template_hash FROM templates WHERE id=?", (tid,))[0][0]
    tick()
    assert _status(tmp, cid) == "scheduled" and packs == []
    _due(tmp, cid)
    tick()
    assert _status(tmp, cid) == "dispatched"
    assert len(packs) == 2
    assert len(_audit(tmp, cid, "campaign.launched")) == 1
    assert _sql(tmp, "SELECT actor FROM audit_event WHERE target=? AND action='campaign.launched'",
                (f"campaign:{cid}",)) == [("system:scheduler",)]
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == []


def test_test_then_schedule_then_tick_dispatches(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "test-first", "<p>Hello</p>")
    before = _sql(tmp, "SELECT test_sent_at, template_hash_at_test FROM campaigns WHERE id=?", (cid,))
    r = _schedule(c, cid)
    assert r.status_code == 200, r.text
    assert r.json() == {"scheduled": True, "scheduled_for": FUTURE_DB}
    assert _sql(tmp, "SELECT test_sent_at, template_hash_at_test FROM campaigns WHERE id=?", (cid,)) == before
    assert _audit(tmp, cid, "campaign.scheduled") == [{"scheduled_for": FUTURE_DB}]
    tick()
    assert _status(tmp, cid) == "scheduled" and packs == []
    _due(tmp, cid)
    tick()
    assert _status(tmp, cid) == "dispatched" and len(packs) == 2
    assert len(_audit(tmp, cid, "campaign.launched")) == 1


@pytest.mark.parametrize("edit", [True, False], ids=["edit", "noedit"])
def test_template_edit_after_schedule(rig, edit):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "edit-after", "<p>Hello</p>")
    assert _schedule(c, cid).status_code == 200
    if edit:
        tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
        r = c.patch(f"/admin/templates/{tid}", json={"name": "T edit-after", "subject": "Hi", "from_name": "Demo Co", "html_body": "<p>Changed & new</p>", "text_body": "x"})
        assert r.status_code == 200, r.text
    _due(tmp, cid)
    tick(2)
    if edit:
        assert _status(tmp, cid) == "failed"
        rows = _audit(tmp, cid, "campaign.scheduled_launch_failed")
        assert len(rows) == 1 and rows[0]["marked_failed"] is True
        assert rows[0]["error"].startswith("template edited since last test")
        assert len(_drift(tmp, f"sched:{cid}")) == 1 and packs == []
        page = _page(c, f"/admin/campaigns/{cid}/analytics")
        assert "The scheduler did not launch this campaign." in page
        assert html.escape(rows[0]["error"]) + ".<div" in page
    else:
        assert _status(tmp, cid) == "dispatched" and len(packs) == 2
        assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == []


def test_untested_schedule_stays_refused(rig):
    from mailman.terminal import routes_admin as ra
    c, ids, tmp, packs, k = rig
    tid = _template(c, "untested", "<p>Hello</p>")
    _contacts(c, "untested", 2)
    cid = _create_scheduled_ui(c, "Untested schedule", tid, ids["eid"])
    assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(FUTURE_DB,)]
    _post(c, f"/admin/campaigns/{cid}/recipients", {"by_tag": "untested"})
    r = _schedule(c, cid)
    assert r.status_code == 400 and r.json()["detail"] == ra._SCHEDULE_NOT_TESTED.format(status="scheduled")
    _due(tmp, cid)
    tick(2)
    assert _status(tmp, cid) == "failed"
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [
        {"error": "test send required before launch", "marked_failed": True}]
    assert "The scheduler did not launch this campaign." in _page(c, f"/admin/campaigns/{cid}/analytics")
    assert packs == []
    good = _create_scheduled_ui(c, "Tested before due", tid, ids["eid"])
    _contacts(c, "untested-good", 2)
    _post(c, f"/admin/campaigns/{good}/recipients", {"by_tag": "untested-good"})
    r = c.post(f"/admin/campaigns/{good}/mark-tested")
    assert r.status_code == 200
    _due(tmp, good)
    tick()
    assert _status(tmp, good) == "dispatched" and len(packs) == 2


@pytest.mark.parametrize("kind", ["draft", "failed", "dispatched", "paused", "cancelled",
                                  "edited", "relaunch_open", "batch_marker", "missing", "twice"])
def test_schedule_refused(rig, kind):
    from mailman.terminal import routes_admin as ra
    c, ids, tmp, packs, k = rig
    cid = _mk(c, f"refused-{kind}", "<p>Hello</p>")
    expected = None
    if kind in ("draft", "failed", "dispatched", "paused", "cancelled"):
        _sql(tmp, "UPDATE campaigns SET status=? WHERE id=?", (kind, cid))
        expected = ra._SCHEDULE_NOT_TESTED.format(status=kind)
    elif kind == "edited":
        tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
        assert c.patch(f"/admin/templates/{tid}", json={"name": f"T refused-{kind}", "subject": "Hi", "from_name": "Demo Co", "html_body": "<p>New</p>", "text_body": "x"}).status_code == 200
        expected = ra._SCHEDULE_EDITED
    elif kind == "relaunch_open":
        _sql(tmp, "UPDATE campaigns SET relaunch_open=1 WHERE id=?", (cid,))
        expected = ra._SCHEDULE_UNFINISHED
    elif kind == "batch_marker":
        _sql(tmp, "INSERT INTO campaign_batches(campaign_id,batch_no,dispatch_started_at) "
                  "VALUES(?,1,datetime('now'))", (cid,))
        expected = ra._SCHEDULE_UNFINISHED
    elif kind == "missing":
        cid = 999999
        expected = "no such campaign"
    elif kind == "twice":
        assert _schedule(c, cid).status_code == 200
        expected = ra._SCHEDULE_NOT_TESTED.format(status="scheduled")
    before = _sql(tmp, "SELECT status, scheduled_for FROM campaigns WHERE id=?", (cid,))
    audits = _audit(tmp, cid, "campaign.scheduled")
    r = _schedule(c, cid)
    assert r.status_code == (404 if kind == "missing" else 400), r.text
    assert r.json()["detail"] == expected
    assert _sql(tmp, "SELECT status, scheduled_for FROM campaigns WHERE id=?", (cid,)) == before
    assert _audit(tmp, cid, "campaign.scheduled") == audits
    assert len(audits) == (1 if kind == "twice" else 0)
    assert packs == []


@pytest.mark.parametrize("at,expected", [
    ("2999-01-01T09:00", FUTURE_DB),
    ("2999-01-01 09:00:00", FUTURE_DB),
    ("2999-01-01T09:00:00Z", FUTURE_DB),
    ("2999-01-01T11:00:00+02:00", FUTURE_DB),
    (" 2999-01-01T09:00 ", FUTURE_DB),
    ("2999-01-01T09:00:00.000Z", FUTURE_DB),
    ("2999-01-01T01:00:00-08:00", FUTURE_DB),
    ("2999-01-01T09:00:00.987654Z", FUTURE_DB),
    ("2999-01-01", "2999-01-01 00:00:00"),
    ("", "BAD_TIME"), ("next tuesday", "BAD_TIME"), ("2999-13-01T09:00", "BAD_TIME"),
    ("x" * 10000, "BAD_TIME"), ("0001-01-01T00:00:00+02:00", "BAD_TIME"),
    ("2999-01-01T09:00:00+25:00", "BAD_TIME"),
    ("0999-01-01T09:00", "PAST"), ("2020-01-01T00:00", "PAST"),
], ids=["naive", "space", "z", "plus02", "trim", "js-z", "minus08", "fraction", "date",
        "empty", "words", "month", "long", "overflow", "bad-offset", "year0999", "past"])
def test_schedule_time_formats(rig, at, expected):
    from mailman.terminal import routes_admin as ra
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "formats", "<p>Hello</p>")
    r = _schedule(c, cid, at)
    if expected in ("BAD_TIME", "PAST"):
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == getattr(ra, "_SCHEDULE_" + expected)
        assert _status(tmp, cid) == "tested"
        assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(None,)]
        assert _audit(tmp, cid, "campaign.scheduled") == []
    else:
        assert r.status_code == 200, r.text
        assert r.json() == {"scheduled": True, "scheduled_for": expected}
        assert _status(tmp, cid) == "scheduled"
        assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(expected,)]
    assert packs == []


def test_campaigns_page_schedule_control(rig):
    c, ids, tmp, packs, k = rig
    a = _mk(c, "control-a", "<p>Hello</p>")
    b = _mk(c, "control-b", "<p>Hello</p>", tested=False)
    cc = _mk(c, "control-c", "<p>Hello</p>")
    assert _schedule(c, cc).status_code == 200
    d = _mk(c, "control-d", "<p>Hello</p>")
    _sql(tmp, "UPDATE campaigns SET status='dispatched' WHERE id=?", (d,))
    e = _mk(c, "control-e", "<p>Hello</p>")
    _sql(tmp, "UPDATE campaigns SET relaunch_open=1 WHERE id=?", (e,))
    f = _mk(c, "control-f", "<p>ENCRYPTED END-TO-END</p>")
    page = _page(c, "/admin/campaigns")
    assert f'data-old-badge="0" onclick="openSchedule({a},this)">Schedule</button>' in page
    assert f'data-old-badge="1" onclick="openSchedule({f},this)">Schedule</button>' in page
    for cid in (b, cc, d, e):
        assert f"openSchedule({cid}," not in page
    for text in ('id="modal-schedule"', 'Launch at <span class="hint">(<span class="tz-label">UTC</span>)</span>',
                 "function saveSchedule(", "'/admin/campaigns/'+_schedId+'/schedule'",
                 "if(verb==='mark-tested'&&pill.textContent==='scheduled')newStatus='scheduled';",
                 'onsubmit="return _utcSubmit(\'cs\',\'cs-utc\')"',
                 "var utc=toUtcIso(document.getElementById('sched-at').value);",
                 "body:JSON.stringify({scheduled_for:utc})"):
        assert text in page, text
    assert "C control-c" in _page(c, "/admin/schedule")


def test_schedule_copy(rig):
    from mailman.terminal import routes_admin as ra
    c, ids, tmp, packs, k = rig
    assert ra._SCHEDULE_BAD_TIME == "Enter a valid date and time to launch."
    assert ra._SCHEDULE_PAST == "Pick a time in the future. To send now, press Launch."
    assert ra._SCHEDULE_NOT_TESTED == ("Only a tested campaign can be scheduled (this one is {status}). "
                                       "Send a test and mark it tested first.")
    assert ra._SCHEDULE_EDITED == ("The template was edited after the last test. Send a test and mark the campaign "
                                   "tested again, then schedule it.")
    assert ra._SCHEDULE_UNFINISHED == "This campaign has an unfinished send. Finish it with Re-launch before scheduling it."
    assert ra._SCHEDULE_RACE == "The campaign changed while it was being scheduled. Reload the page and try again."
    docs = _page(c, "/admin/docs")
    assert ("Set <b>Schedule</b> when creating a campaign, or press <b>Schedule</b> on a tested campaign, "
            "to launch it automatically at that time. Times are entered and shown in your browser's time zone "
            "and stored in UTC. It launches only if it has been marked tested by then: marking a scheduled "
            "campaign tested keeps it scheduled. Pause takes it off the schedule; after Resume, schedule it again.") in docs
    assert "Fix the cause, mark it tested again, then launch or schedule it." in docs
    campaigns = _page(c, "/admin/campaigns")
    assert "mark tested -> launch now or schedule it. The template-hash gate" in campaigns
    assert 'optional (<span class="tz-label">UTC</span>)' in campaigns
    schedule = _page(c, "/admin/schedule")
    assert "Times are shown in your browser's time zone." in schedule
    assert "local to the terminal" not in schedule
    cid = _mk(c, "copy-refusal", "<p>{{previewText}}</p>")
    assert _schedule(c, cid).status_code == 200
    _due(tmp, cid)
    tick()
    assert "tested, then launch or schedule it from the Campaigns page." in _page(c, f"/admin/campaigns/{cid}/analytics")


def test_pause_resume_cancel_with_schedule(rig):
    from mailman.terminal import routes_admin as ra
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "pause-flow", "<p>Hello</p>")
    assert _schedule(c, cid).status_code == 200
    r = c.post(f"/admin/campaigns/{cid}/pause")
    assert r.status_code == 200 and r.json() == {"paused": True}
    assert _status(tmp, cid) == "paused"
    _due(tmp, cid)
    tick()
    assert _status(tmp, cid) == "paused" and packs == []
    r = c.post(f"/admin/campaigns/{cid}/resume")
    assert r.status_code == 200 and r.json() == {"resumed": True, "status": "tested"}
    tick()
    assert _status(tmp, cid) == "tested" and packs == []
    assert _schedule(c, cid).status_code == 200
    _due(tmp, cid)
    tick()
    assert _status(tmp, cid) == "dispatched"
    packs.clear()
    other = _mk(c, "cancel-flow", "<p>Hello</p>")
    assert _schedule(c, other).status_code == 200
    assert c.post(f"/admin/campaigns/{other}/cancel").status_code == 200
    _due(tmp, other)
    tick()
    assert _status(tmp, other) == "cancelled" and packs == []
    r = _schedule(c, other)
    assert r.status_code == 400 and r.json()["detail"] == ra._SCHEDULE_NOT_TESTED.format(status="cancelled")


def test_failed_campaign_rescheduled_after_retest(rig):
    from mailman.terminal import routes_admin as ra
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "failed-retest", "<p>{{previewText}}</p>")
    assert _schedule(c, cid).status_code == 200
    _due(tmp, cid)
    tick()
    assert _status(tmp, cid) == "failed"
    r = _schedule(c, cid)
    assert r.status_code == 400 and r.json()["detail"] == ra._SCHEDULE_NOT_TESTED.format(status="failed")
    tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
    assert c.patch(f"/admin/templates/{tid}", json={"name": "T failed-retest", "subject": "Hi", "from_name": "Demo Co", "html_body": "<p>Hello</p>", "text_body": "x"}).status_code == 200
    assert c.post(f"/admin/campaigns/{cid}/mark-tested").status_code == 200
    assert _status(tmp, cid) == "tested"
    assert _schedule(c, cid).status_code == 200
    _due(tmp, cid)
    tick()
    assert _status(tmp, cid) == "dispatched" and len(packs) == 2
    assert len(_audit(tmp, cid, "campaign.scheduled_launch_failed")) == 1
    assert len(_audit(tmp, cid, "campaign.launched")) == 1


def test_stress_mixed_schedule_flows(rig):
    c, ids, tmp, packs, k = rig
    rng = random.Random(11)
    kinds = ["sched_then_test", "test_then_sched", "untested_sched", "edited_after_sched", "future", "paused"]
    campaigns = []
    for i in range(30):
        kind, n = rng.choice(kinds), rng.randint(1, 3)
        tag = f"stress-{i}"
        if kind in ("sched_then_test", "untested_sched"):
            tid = _template(c, tag, "<p>Hello</p>")
            _contacts(c, tag, n)
            cid = _create_scheduled_ui(c, f"C {tag}", tid, ids["eid"])
            _post(c, f"/admin/campaigns/{cid}/recipients", {"by_tag": tag})
            if kind == "sched_then_test":
                assert c.post(f"/admin/campaigns/{cid}/mark-tested").status_code == 200
        else:
            cid = _mk(c, tag, "<p>Hello</p>", n=n)
            assert _schedule(c, cid).status_code == 200
            if kind == "test_then_sched":
                assert _schedule(c, cid).status_code == 400
            elif kind == "edited_after_sched":
                tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
                assert c.patch(f"/admin/templates/{tid}", json={"name": f"T {tag}", "subject": "Hi", "from_name": "Demo Co", "html_body": "<p>Changed</p>", "text_body": "x"}).status_code == 200
            elif kind == "paused":
                assert c.post(f"/admin/campaigns/{cid}/pause").status_code == 200
        if kind != "future":
            _due(tmp, cid)
        campaigns.append((cid, kind, n))
    tick(3)
    total = 0
    for cid, kind, n in campaigns:
        if kind in ("sched_then_test", "test_then_sched"):
            assert _status(tmp, cid) == "dispatched"
            assert len(_audit(tmp, cid, "campaign.launched")) == 1
            total += n
        elif kind in ("untested_sched", "edited_after_sched"):
            assert _status(tmp, cid) == "failed"
            rows = _audit(tmp, cid, "campaign.scheduled_launch_failed")
            assert len(rows) == 1 and rows[0]["marked_failed"] is True
            if kind == "untested_sched":
                assert rows[0]["error"] == "test send required before launch"
            else:
                assert rows[0]["error"].startswith("template edited since last test")
        else:
            assert _status(tmp, cid) == ("scheduled" if kind == "future" else "paused")
            assert _audit(tmp, cid, "campaign.launched") == []
    assert len(packs) == total == len({p["recipient_hash"] for p in packs})
    audits = _sql(tmp, "SELECT COUNT(*) FROM audit_event")
    tick(2)
    assert len(packs) == total and _sql(tmp, "SELECT COUNT(*) FROM audit_event") == audits


@pytest.mark.parametrize("value,expected", [
    ("2030-01-15T09:00+02:00", "2030-01-15 07:00:00"),
    ("2030-01-15T07:00:00.000Z", "2030-01-15 07:00:00"),
    ("2030-07-15T09:00:00-07:00", "2030-07-15 16:00:00"),
    ("2026-12-01T09:00:00Z", "2026-12-01 09:00:00"),
    ("2026-10-12 09:00", "2026-10-12 09:00:00"),
    ("2026-10-12T09:00", "2026-10-12 09:00:00"),
    ("0999-01-01T00:00", "0999-01-01 00:00:00"),
    ("2030-01-15T23:30:00-01:00", "2030-01-16 00:30:00"),
    ("0001-01-01T00:00:00+02:00", None),
    ("2030-01-15T09:00:00+25:00", None), ("nope", None), ("", None),
], ids=["plus02", "js-z", "minus07", "legacy-z", "old-ui", "naive", "year0999",
        "rollover", "overflow", "bad-offset", "garbage", "empty"])
def test_utc_helpers(value, expected):
    from mailman.terminal.routes_admin import parse_utc_naive, utc_text
    if expected is None:
        with pytest.raises(ValueError):
            parse_utc_naive(value)
    else:
        assert utc_text(parse_utc_naive(value)) == expected


@pytest.mark.parametrize("value,expected", [
    ("2999-01-01T07:00:00.000Z", "2999-01-01 07:00:00"),
    (FUTURE, FUTURE_DB), ("", None), ("garbage", "invalid"),
], ids=["js-z", "naive", "empty", "garbage"])
def test_create_form_normalises_time(rig, value, expected):
    c, ids, tmp, packs, k = rig
    tid = _template(c, "create-normalise", "<p>Hello</p>")
    r = c.post("/admin/campaigns/create", data={"name": "Normalised create", "template_id": tid,
               "entity_id": ids["eid"], "scheduled_for": value}, follow_redirects=False)
    assert r.status_code == 303
    rows = _sql(tmp, "SELECT status, scheduled_for FROM campaigns WHERE name='Normalised create'")
    assert rows == ([] if expected == "invalid" else [("draft" if expected is None else "scheduled", expected)])


@pytest.mark.parametrize("value,expected", [
    ("2999-01-01T07:00:00.000Z", "2999-01-01 07:00:00"),
    ("2999-01-01T11:00:00+02:00", FUTURE_DB), (FUTURE, FUTURE_DB),
    ("garbage", None), ("", None),
], ids=["js-z", "plus02", "naive", "garbage", "empty"])
def test_stage_form_normalises_time(rig, value, expected):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-normalise", "<p>Hello</p>")
    tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
    c.post(f"/admin/campaigns/{cid}/stages/add",
           data={"template_id": tid, "scheduled_for": value}, follow_redirects=False)
    rows = _sql(tmp, "SELECT scheduled_for FROM campaign_stages WHERE campaign_id=?", (cid,))
    assert rows == ([] if expected is None else [(expected,)])


def test_local_time_markup(rig):
    c, ids, tmp, packs, k = rig
    markup = '<time datetime="2030-01-15T07:00:00Z" data-local>2030-01-15 07:00</time>'
    cid = _mk(c, "local-markup", "<p>Hello</p>")
    for value in ("2030-01-15 07:00:00", "2030-01-15T07:00:00Z",
                  "2030-01-15T09:00:00+02:00", "2030-01-15 07:00"):
        _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for=?, dispatched_at=? WHERE id=?",
             (value, value, cid))
        for path in ("/admin/campaigns", "/admin/schedule"):
            page = _page(c, path)
            assert page.count(markup) == 2
            assert '<th>Scheduled (<span class="tz-label">UTC</span>)</th>' in page
            assert '<th>Dispatched (<span class="tz-label">UTC</span>)</th>' in page
    _sql(tmp, "UPDATE campaigns SET scheduled_for='not a date', dispatched_at=NULL WHERE id=?", (cid,))
    for path in ("/admin/campaigns", "/admin/schedule"):
        page = _page(c, path)
        assert "<td class=\"cell-dim\">not a date</td>" in page
        assert '<td class="cell-dim">-</td>' in page
        assert ' data-local>' not in page
    _sql(tmp, "UPDATE campaigns SET scheduled_for=NULL WHERE id=?", (cid,))
    for path in ("/admin/campaigns", "/admin/schedule"):
        assert '<td class="cell-dim">-</td>' in _page(c, path)
    tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
    _post(c, f"/admin/campaigns/{cid}/stages", {"template_id": tid, "scheduled_for": "2030-01-15 07:00:00"})
    for path in ("/admin/campaigns", "/admin/schedule", f"/admin/campaigns/{cid}/recipients"):
        page = _page(c, path)
        assert page.count("function toUtcIso(") == 1
        if path.endswith("/recipients"):
            assert markup in page


def test_offset_time_fires_at_utc_instant(rig):
    from mailman.terminal.routes_admin import utc_text
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "offset-fire", "<p>Hello</p>")
    target = datetime.now(timezone.utc) + timedelta(minutes=10)
    at = target.astimezone(timezone(timedelta(hours=-8))).isoformat()
    r = _schedule(c, cid, at)
    assert r.status_code == 200
    assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(utc_text(target.replace(tzinfo=None)),)]
    tick()
    assert _status(tmp, cid) == "scheduled" and packs == []
    _due(tmp, cid)
    tick()
    assert _status(tmp, cid) == "dispatched" and len(packs) == 2


def test_mark_tested_and_schedule_restart_streak(rig, monkeypatch, clock):
    from mailman.terminal import launch
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "streak-restart", "<p>Hello</p>")
    _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for='2026-01-01 09:00:00', "
              "updated_at=datetime('now','-1 minute') WHERE id=?", (cid,))

    async def broken(bearer):
        raise httpx.ConnectError("A (test)")

    monkeypatch.setattr(launch, "_ensure_k_temp", broken)
    tick()
    clock(hours=20)
    r = c.post(f"/admin/campaigns/{cid}/mark-tested")
    assert r.status_code == 200 and r.json()["tested"] is True
    assert _status(tmp, cid) == "scheduled"
    tick()
    rows = _audit(tmp, cid, "campaign.scheduled_launch_retrying")
    assert [r["streak_start"] for r in rows] == [_S(), _S(hours=20)]
    clock(hours=43, minutes=59, seconds=59)
    tick()
    assert _status(tmp, cid) == "scheduled"
    clock(hours=44)
    tick()
    assert _status(tmp, cid) == "failed"
    _age(tmp, cid, "-1 minute")
    r = c.post(f"/admin/campaigns/{cid}/mark-tested")
    assert r.status_code == 200 and _status(tmp, cid) == "tested"
    r = _schedule(c, cid)
    assert r.status_code == 200 and r.json() == {"scheduled": True, "scheduled_for": FUTURE_DB}
    assert _status(tmp, cid) == "scheduled"
    assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(FUTURE_DB,)]
    page = _page(c, f"/admin/campaigns/{cid}/analytics")
    assert "The scheduler did not launch this campaign." not in page
    assert "has not launched this campaign yet" not in page
