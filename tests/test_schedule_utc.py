"""Scheduled campaigns, stages and API inputs use UTC instants consistently."""
from __future__ import annotations
import asyncio, json, random, re, sqlite3
from datetime import datetime, timedelta, timezone
import pytest


REASON = "its scheduled time is not a valid date and time"
ZRE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


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
    main.app.dependency_overrides[auth.require_session] = lambda: {"sub": "scheduler@acme.com", "role": "admin"}
    c = TestClient(main.app)
    settings.set("email_footer_address", "1 Example Street")
    eid = _post(c, "/admin/entities", {
        "name": "Demo Co", "domain": "acme.com", "from_name": "Demo Co",
        "from_email": "hello@acme.com", "footer_address": "1 Example Street",
        "support_email": "support@acme.com", "public_host": "localhost"})["id"]
    (keys / "cloud_bearer.txt").write_text("tok")
    k = encryption.mint_k_temp()
    packs = []

    async def _k(bearer):
        return k

    def _creds(entity_id):
        return {"resend_key": "re_x1", "from_name": "Demo Co",
                "from_email": "hello@acme.com", "domain": "acme.com"}

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
        _post(c, "/admin/contacts", {"email": f"{tag}{i}@acme.com", "name": f"Person {i}",
                                    "tags": tag, "consent_source": "demo"})
    tid = _template(c, tag, html)
    with db.session() as s:
        eid = s.execute(select(Entity.id).where(Entity.name == "Demo Co")).scalar_one()
    cid = _post(c, "/admin/campaigns", {"name": f"C {tag}", "template_id": tid, "entity_id": eid})["id"]
    _post(c, f"/admin/campaigns/{cid}/recipients", {"by_tag": tag})
    routes_admin.mark_tested(cid)
    return cid


def _audit(tmp, cid, action):
    return [json.loads(r[0]) for r in _sql(
        tmp, "SELECT details_json FROM audit_event WHERE target=? AND action=? ORDER BY id",
        (f"campaign:{cid}", action))]


def _drift(tmp, job_id):
    return _sql(tmp, "SELECT severity, category, details_json FROM drift_report WHERE job_id=? ORDER BY id", (job_id,))


def _status(tmp, cid):
    return _sql(tmp, "SELECT status FROM campaigns WHERE id=?", (cid,))[0][0]


def _stage_status(tmp, sid):
    return _sql(tmp, "SELECT status FROM campaign_stages WHERE id=?", (sid,))[0][0]


def _page(c, path):
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def _at(delta_s):
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=delta_s)


FORMS = {
    "z": lambda when: when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "t": lambda when: when.strftime("%Y-%m-%dT%H:%M:%S"),
    "plus2": lambda when: when.astimezone(timezone(timedelta(hours=2))).isoformat(timespec="seconds"),
    "minus8": lambda when: when.astimezone(timezone(timedelta(hours=-8))).isoformat(timespec="seconds"),
    "hhmm": lambda when: when.strftime("%Y-%m-%d %H:%M"),
    "canon": lambda when: when.strftime("%Y-%m-%d %H:%M:%S"),
}


def _stage_sql(tmp, cid, n, tid, value, note=None):
    _sql(tmp, "INSERT INTO campaign_stages(campaign_id, stage_number, template_id, scheduled_for, note) "
              "VALUES (?, ?, ?, ?, ?)", (cid, n, tid, value, note))
    return _sql(tmp, "SELECT id FROM campaign_stages WHERE campaign_id=? AND stage_number=?", (cid, n))[0][0]


@pytest.mark.parametrize("value,expected", [
    pytest.param("2026-12-01T09:00:00Z", "2026-12-01 09:00:00", id="z"),
    pytest.param("2026-12-01T09:00:00", "2026-12-01 09:00:00", id="t"),
    pytest.param("2026-12-01T11:00:00+02:00", "2026-12-01 09:00:00", id="plus2"),
    pytest.param("2026-12-01T01:00:00-08:00", "2026-12-01 09:00:00", id="minus8"),
    pytest.param("2026-12-01 09:00", "2026-12-01 09:00:00", id="hhmm"),
    pytest.param("2026-12-01T09:00:00.000Z", "2026-12-01 09:00:00", id="fraction"),
    pytest.param("2026-12-01 09:00:00", "2026-12-01 09:00:00", id="canon"),
    pytest.param("garbage", None, id="garbage"),
    pytest.param("", None, id="empty"),
])
def test_sqlite_time_compare(value, expected):
    con = sqlite3.connect(":memory:")
    try:
        row = con.execute(
            "SELECT datetime(?), datetime(?) <= datetime('2026-12-01 08:59:59'), "
            "datetime(?) <= datetime('2026-12-01 09:00:00')", (value, value, value)).fetchone()
        assert row == ((expected, 0, 1) if expected is not None else (None, None, None))
        if value == "2026-12-01T09:00:00Z":
            assert con.execute("SELECT ? <= ?", (value, "2026-12-01 09:30:00")).fetchone() == (0,)
    finally:
        con.close()


def test_campaign_forms_fire_at_the_right_minute(rig):
    c, ids, tmp, packs, k = rig
    campaigns = [(form, due, _mk(c, f"campaign-{form}-{due}", "<p>Hello {{name}}</p>", n=1))
                 for form in FORMS for due in (True, False)]
    now = _at(0)
    for form, due, cid in campaigns:
        value = FORMS[form](now + timedelta(seconds=-90 if due else 150))
        _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for=? WHERE id=?", (value, cid))
    tick()
    for form, due, cid in campaigns:
        assert _status(tmp, cid) == ("dispatched" if due else "scheduled"), (form, due)
        assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == []
    assert len(packs) == 6
    assert all(ZRE.fullmatch(p["send_at"]) for p in packs)


def test_stage_forms_fire_at_the_right_minute(rig):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-forms", "<p>Hello {{name}}</p>", n=1)
    tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
    now = _at(0)
    stages = []
    for number, (form, due) in enumerate(((form, due) for form in FORMS for due in (True, False)), 1):
        value = FORMS[form](now + timedelta(seconds=-90 if due else 150))
        stages.append((form, due, _stage_sql(tmp, cid, number, tid, value)))
    tick()
    for form, due, sid in stages:
        assert _stage_status(tmp, sid) == ("dispatched" if due else "pending"), (form, due)
    assert len(packs) == 6
    assert all(p["send_at"] == "1970-01-01T00:00:00Z" for p in packs)
    assert _audit(tmp, cid, "campaign.stage_failed") == []


@pytest.mark.parametrize("value", [
    pytest.param("garbage", id="garbage"),
    pytest.param("", id="empty"),
])
def test_unparseable_times_fail_once(rig, value):
    from mailman.terminal import sync_client
    c, ids, tmp, packs, k = rig
    assert sync_client._UNPARSEABLE_TIME == REASON
    a = _mk(c, "bad-time-campaign", "<p>Hello {{name}}</p>", n=1)
    b = _mk(c, "bad-time-stage", "<p>Hello {{name}}</p>", n=1)
    tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (b,))[0][0]
    _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for=? WHERE id=?", (value, a))
    sid = _stage_sql(tmp, b, 1, tid, value)
    tick()
    assert _status(tmp, a) == "failed"
    assert _audit(tmp, a, "campaign.scheduled_launch_failed") == [{"error": REASON, "marked_failed": True}]
    drift = _drift(tmp, f"sched:{a}")
    assert len(drift) == 1
    assert drift[0][:2] == ("WARN", "status_mismatch")
    assert json.loads(drift[0][2]) == {"campaign_id": a, "error": REASON, "marked_failed": True}
    assert _stage_status(tmp, sid) == "failed"
    audit = _audit(tmp, b, "campaign.stage_failed")
    assert len(audit) == 1
    assert audit[0]["stage_id"] == sid
    assert audit[0]["error"] == REASON
    assert not audit[0].get("possibly_sent")
    drift = _drift(tmp, f"stage:{sid}")
    assert len(drift) == 1
    details = json.loads(drift[0][2])
    assert details["campaign_id"] == b
    assert details["stage_id"] == sid
    assert details["error"] == REASON
    assert _status(tmp, b) == "tested"
    assert packs == []
    for cid in (a, b):
        assert _audit(tmp, cid, "campaign.scheduled_launch_retrying") == []
        assert _audit(tmp, cid, "campaign.stage_retrying") == []
    page = _page(c, f"/admin/campaigns/{a}/analytics")
    assert "The scheduler did not launch this campaign." in page
    assert REASON in page
    page = _page(c, f"/admin/campaigns/{b}/recipients")
    assert f"Not sent: {REASON}. Delete this stage" in page
    assert "may already be on its way" not in page
    audit_counts = _sql(tmp, "SELECT target, COUNT(*) FROM audit_event WHERE target IN (?, ?) GROUP BY target",
                        (f"campaign:{a}", f"campaign:{b}"))
    drift_count = _sql(tmp, "SELECT COUNT(*) FROM drift_report")
    tick(2)
    assert _sql(tmp, "SELECT target, COUNT(*) FROM audit_event WHERE target IN (?, ?) GROUP BY target",
                (f"campaign:{a}", f"campaign:{b}")) == audit_counts
    assert _sql(tmp, "SELECT COUNT(*) FROM drift_report") == drift_count
    assert packs == []


def test_unparseable_left_when_not_due_otherwise(rig):
    c, ids, tmp, packs, k = rig
    tid = _template(c, "not-due-draft", "<p>Hello</p>")
    a = _post(c, "/admin/campaigns", {"name": "Not due draft", "template_id": tid, "entity_id": ids["eid"]})["id"]
    b = _mk(c, "not-due-paused", "<p>Hello {{name}}</p>", n=1)
    future = _mk(c, "not-due-future", "<p>Hello {{name}}</p>", n=1)
    tid_b = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (b,))[0][0]
    _sql(tmp, "UPDATE campaigns SET scheduled_for='garbage' WHERE id=?", (a,))
    _sql(tmp, "UPDATE campaigns SET status='paused' WHERE id=?", (b,))
    _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for='2999-01-01 00:00:00' WHERE id=?", (future,))
    sid = _stage_sql(tmp, b, 1, tid_b, "garbage")
    tick(2)
    assert _status(tmp, a) == "draft"
    assert _status(tmp, b) == "paused"
    assert _status(tmp, future) == "scheduled"
    assert _stage_status(tmp, sid) == "pending"
    for cid in (a, b, future):
        assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == []
        assert _audit(tmp, cid, "campaign.stage_failed") == []
    assert packs == []


def test_unparseable_refusal_keeps_operator_change(rig):
    from mailman.terminal import sync_client
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "changed-time", "<p>Hello {{name}}</p>", n=1)
    _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for='garbage2' WHERE id=?", (cid,))
    assert sync_client._refuse_campaign(cid, REASON, unparseable="garbage") is False
    assert _status(tmp, cid) == "scheduled"
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed")[-1]["marked_failed"] is False
    _sql(tmp, "UPDATE campaigns SET status='paused', scheduled_for='garbage' WHERE id=?", (cid,))
    assert sync_client._refuse_campaign(cid, REASON, unparseable="garbage") is False
    assert _status(tmp, cid) == "paused"
    _sql(tmp, "UPDATE campaigns SET status='scheduled' WHERE id=?", (cid,))
    assert sync_client._refuse_campaign(cid, REASON, unparseable="garbage") is True
    assert _status(tmp, cid) == "failed"


def test_refusal_guard_compares_time(rig):
    c, ids, tmp, packs, k = rig
    tid = _template(c, "untested-time", "<p>Hello</p>")
    cid = _post(c, "/admin/campaigns", {"name": "Untested time", "template_id": tid, "entity_id": ids["eid"]})["id"]
    value = FORMS["z"](_at(-90))
    _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for=? WHERE id=?", (value, cid))
    tick(2)
    assert _status(tmp, cid) == "failed"
    assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [
        {"error": "test send required before launch", "marked_failed": True}]


@pytest.mark.parametrize("value,expected", [
    pytest.param(None, "1970-01-01T00:00:00Z", id="null"),
    pytest.param("", "1970-01-01T00:00:00Z", id="empty"),
    pytest.param("  ", "1970-01-01T00:00:00Z", id="space"),
    pytest.param("2026-01-01 09:00", "2026-01-01T09:00:00Z", id="hhmm"),
    pytest.param("2026-01-01 09:00:00", "2026-01-01T09:00:00Z", id="canon"),
    pytest.param("2026-01-01T09:00:00Z", "2026-01-01T09:00:00Z", id="z"),
    pytest.param("2026-01-01T11:00:00+02:00", "2026-01-01T09:00:00Z", id="plus2"),
    pytest.param("2026-01-01T09:00:00.987654Z", "2026-01-01T09:00:00Z", id="fraction"),
    pytest.param("0999-01-01T00:00", "0999-01-01T00:00:00Z", id="0999"),
    pytest.param("garbage", "1970-01-01T00:00:00Z", id="garbage"),
])
def test_pack_send_at_helper(value, expected):
    from mailman.terminal import launch
    result = launch._pack_send_at(value)
    assert result == expected
    assert ZRE.fullmatch(result)


@pytest.mark.parametrize("value,expected", [
    pytest.param("2026-01-01T11:00:00+02:00", "2026-01-01T09:00:00Z", id="plus2"),
    pytest.param(None, "1970-01-01T00:00:00Z", id="null"),
])
def test_stage_packs_carry_z_send_at(rig, value, expected):
    c, ids, tmp, packs, k = rig
    cid = _mk(c, "stage-pack-time", "<p>Hello {{name}}</p>", n=1)
    tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (cid,))[0][0]
    stage_time = FORMS["z"](_at(-90))
    _sql(tmp, "UPDATE campaigns SET scheduled_for=? WHERE id=?", (value, cid))
    sid = _stage_sql(tmp, cid, 1, tid, stage_time)
    tick()
    assert _stage_status(tmp, sid) == "dispatched"
    assert len(packs) == 1
    assert all(p["send_at"] == expected for p in packs)


@pytest.mark.parametrize("route", ["create", "edit", "add_stage", "update_stage"])
@pytest.mark.parametrize("value,expected", [
    pytest.param("2030-01-15T09:00:00Z", "2030-01-15 09:00:00", id="z"),
    pytest.param("2030-01-15T11:00:00+02:00", "2030-01-15 09:00:00", id="plus2"),
    pytest.param("2030-01-15T01:00:00-08:00", "2030-01-15 09:00:00", id="minus8"),
    pytest.param("2030-01-15 09:00", "2030-01-15 09:00:00", id="hhmm"),
    pytest.param("2030-01-15T09:00", "2030-01-15 09:00:00", id="t-hhmm"),
    pytest.param("2020-01-01T00:00:00Z", "2020-01-01 00:00:00", id="past"),
    pytest.param("", None, id="empty"),
    pytest.param("garbage", None, id="garbage"),
    pytest.param("2030-13-01T09:00", None, id="bad-month"),
    pytest.param("2030-01-15T09:00:00+25:00", None, id="bad-offset"),
    pytest.param("x" * 10000, None, id="long"),
])
def test_json_routes_store_utc(rig, route, value, expected):
    from mailman.terminal import routes_admin
    c, ids, tmp, packs, k = rig
    tid = _template(c, "route-time", "<p>Hello</p>")
    name = "Posted schedule time"
    cid = sid = None
    if route != "create":
        cid = _post(c, "/admin/campaigns", {"name": name, "template_id": tid, "entity_id": ids["eid"]})["id"]
    request_tid = tid
    if route == "update_stage":
        sid = _stage_sql(tmp, cid, 1, tid, "2999-01-01 00:00:00", note="keep")
        request_tid = _template(c, "route-time-update", "<p>Updated</p>")
    if route == "create":
        response = c.post("/admin/campaigns", json={"name": name, "template_id": tid,
                                                   "entity_id": ids["eid"], "scheduled_for": value})
    elif route == "edit":
        response = c.patch(f"/admin/campaigns/{cid}", json={"scheduled_for": value})
    elif route == "add_stage":
        response = c.post(f"/admin/campaigns/{cid}/stages", json={"template_id": request_tid,
                                                                 "scheduled_for": value, "note": "change"})
    else:
        response = c.patch(f"/admin/campaigns/{cid}/stages/{sid}", json={"template_id": request_tid,
                                                                       "scheduled_for": value, "note": "change"})
    if expected is None:
        assert response.status_code == 400, response.text[:300]
        assert response.json()["detail"] == routes_admin._SCHEDULED_FOR_INVALID
        if route == "create":
            assert _sql(tmp, "SELECT COUNT(*) FROM campaigns WHERE name=?", (name,)) == [(0,)]
        elif route == "edit":
            assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(None,)]
            assert _audit(tmp, cid, "campaign.edited") == []
        elif route == "add_stage":
            assert _sql(tmp, "SELECT COUNT(*) FROM campaign_stages WHERE campaign_id=?", (cid,)) == [(0,)]
        else:
            assert _sql(tmp, "SELECT scheduled_for, note, template_id FROM campaign_stages WHERE id=?",
                        (sid,)) == [("2999-01-01 00:00:00", "keep", tid)]
    else:
        assert response.status_code == 200, response.text[:300]
        if route == "create":
            cid = response.json()["id"]
        elif route == "add_stage":
            sid = response.json()["id"]
        if route in ("create", "edit"):
            assert _sql(tmp, "SELECT scheduled_for, status FROM campaigns WHERE id=?",
                        (cid,)) == [(expected, "scheduled" if route == "create" else "draft")]
        else:
            assert _sql(tmp, "SELECT scheduled_for FROM campaign_stages WHERE id=?", (sid,)) == [(expected,)]


@pytest.mark.parametrize("case", ["omitted", "null"])
def test_create_without_time_is_draft(rig, case):
    c, ids, tmp, packs, k = rig
    tid = _template(c, "no-time", "<p>Hello</p>")
    body = {"name": "No scheduled time", "template_id": tid, "entity_id": ids["eid"]}
    if case == "null":
        body["scheduled_for"] = None
    response = c.post("/admin/campaigns", json=body)
    assert response.status_code == 200, response.text[:300]
    cid = response.json()["id"]
    assert _status(tmp, cid) == "draft"
    assert _sql(tmp, "SELECT scheduled_for FROM campaigns WHERE id=?", (cid,)) == [(None,)]


def test_schedule_page_orders_by_time(rig):
    c, ids, tmp, packs, k = rig
    values = {"Order-A": "2030-01-15T10:00:00Z", "Order-B": "2030-01-15 09:30:00",
              "Order-C": "2030-01-15T11:00:00+02:00"}
    campaigns = [(name, _mk(c, name, "<p>Hello {{name}}</p>", n=1)) for name in values]
    for name, cid in campaigns:
        _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for=? WHERE id=?", (values[name], cid))
    page = _page(c, "/admin/schedule")
    assert page.index("Order-C") < page.index("Order-B") < page.index("Order-A")


def test_stress_mixed_time_forms(rig):
    c, ids, tmp, packs, k = rig
    rng = random.Random(27)
    forms = tuple(FORMS)
    deltas = (-3600, -600, -61, 121, 600, 3600)
    campaigns = [(rng.choice(forms), rng.choice(deltas),
                  _mk(c, f"mixed-campaign-{i}", "<p>Hello {{name}}</p>", n=1)) for i in range(30)]
    host = _mk(c, "mixed-stage-host", "<p>Hello {{name}}</p>", n=1)
    bad_campaigns = [(value, _mk(c, f"mixed-bad-{i}", "<p>Hello {{name}}</p>", n=1))
                     for i, value in enumerate(("garbage", "garbage", "", ""))]
    tid = _sql(tmp, "SELECT template_id FROM campaigns WHERE id=?", (host,))[0][0]
    stage_specs = [(rng.choice(forms), rng.choice(deltas)) for _ in range(20)]
    now = _at(0)
    for form, delta, cid in campaigns:
        _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for=? WHERE id=?",
             (FORMS[form](now + timedelta(seconds=delta)), cid))
    for value, cid in bad_campaigns:
        _sql(tmp, "UPDATE campaigns SET status='scheduled', scheduled_for=? WHERE id=?", (value, cid))
    stages = [(delta, _stage_sql(tmp, host, number, tid, FORMS[form](now + timedelta(seconds=delta))))
              for number, (form, delta) in enumerate(stage_specs, 1)]
    bad_stages = [_stage_sql(tmp, host, number, tid, "garbage") for number in (21, 22)]
    tick(3)
    for form, delta, cid in campaigns:
        assert _status(tmp, cid) == ("dispatched" if delta < 0 else "scheduled"), (form, delta, cid)
        assert len(_audit(tmp, cid, "campaign.launched")) == (1 if delta < 0 else 0)
    for delta, sid in stages:
        assert _stage_status(tmp, sid) == ("dispatched" if delta < 0 else "pending"), (delta, sid)
    for value, cid in bad_campaigns:
        assert _status(tmp, cid) == "failed"
        assert _audit(tmp, cid, "campaign.scheduled_launch_failed") == [{"error": REASON, "marked_failed": True}]
    stage_audit = _audit(tmp, host, "campaign.stage_failed")
    assert len(stage_audit) == 2
    for sid in bad_stages:
        assert _stage_status(tmp, sid) == "failed"
        matching = [d for d in stage_audit if d["stage_id"] == sid]
        assert len(matching) == 1
        assert matching[0]["error"] == REASON
    expected = sum(delta < 0 for form, delta, cid in campaigns) + sum(delta < 0 for delta, sid in stages)
    assert len(packs) == expected
    assert all(ZRE.fullmatch(p["send_at"]) for p in packs)
    audit_count = _sql(tmp, "SELECT COUNT(*) FROM audit_event")
    drift_count = _sql(tmp, "SELECT COUNT(*) FROM drift_report")
    tick(2)
    assert len(packs) == expected
    assert _sql(tmp, "SELECT COUNT(*) FROM audit_event") == audit_count
    assert _sql(tmp, "SELECT COUNT(*) FROM drift_report") == drift_count
