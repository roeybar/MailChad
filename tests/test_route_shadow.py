"""Pin route delegation and the shared HTML page routes."""
import re
from types import SimpleNamespace

import pytest


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import insert
    from mailman.terminal import admin_ui, auth, db, encryption, main, routes_admin, settings
    from mailman.terminal.models import CampaignRecipient

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.sqlite")
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setattr(encryption, "KEYS_DIR", keys)
    monkeypatch.setattr(settings, "KEYS_DIR", keys)
    monkeypatch.setattr(settings, "KEK_PATH", keys / "settings_kek.bin")
    monkeypatch.setattr(settings, "_kek", None)
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    db.init()

    reference_app = FastAPI()
    reference_app.include_router(routes_admin.router)
    session = lambda: {"sub": "route-shadow@example.com", "role": "admin"}
    overrides = main.app.dependency_overrides.copy()
    original_routes = main.app.router.routes[:]
    main.app.dependency_overrides[auth.require_session] = session
    reference_app.dependency_overrides[auth.require_session] = session
    client = TestClient(main.app, follow_redirects=False, raise_server_exceptions=False)
    reference = TestClient(reference_app, follow_redirects=False, raise_server_exceptions=False)
    try:
        entity = routes_admin.create_entity(routes_admin.EntityIn(
            name="Shadow Co", domain="shadow.example", from_name="Shadow Co",
            from_email="hello@shadow.example"))["id"]
        contact = routes_admin.create_contact(routes_admin.ContactIn(
            email="contact@shadow.example", name="Shadow Contact", tags="shadow-tag"))["id"]
        template = routes_admin.create_template(routes_admin.TemplateIn(
            name="Shadow Template", subject="Hello", from_name="Shadow Co",
            html_body="<p>Hello</p>", text_body="Hello"))["id"]
        draft = routes_admin.create_campaign(routes_admin.CampaignIn(
            name="Shadow Draft", template_id=template, entity_id=entity))["id"]
        tested = routes_admin.create_campaign(routes_admin.CampaignIn(
            name="Shadow Tested", template_id=template, entity_id=entity))["id"]
        routes_admin.mark_tested(tested)
        # The page must render the retry control, not merely its function definition.
        with db.session() as s:
            s.execute(insert(CampaignRecipient).values(
                campaign_id=tested, contact_id=contact, status="failed",
                failure_reason="injected").execution_options(synchronize_session=False))
            s.commit()
        yield SimpleNamespace(client=client, reference=reference, main=main, admin_ui=admin_ui,
                              api=routes_admin, db=db, entity=entity, contact=contact,
                              template=template, draft=draft, tested=tested)
    finally:
        client.close()
        reference.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        main.app.router.routes[:] = original_routes


def _same_refusal(actual, expected):
    assert actual.status_code != 303
    assert actual.headers["content-type"].startswith("application/json")
    assert actual.status_code == expected.status_code
    if expected.status_code == 422 and any(
            d.get("loc", [None])[0] == "path" for d in expected.json().get("detail", [])
            if isinstance(d, dict)):
        # Pinned deliberate difference: wrapper/API parameter names differ;
        # renaming them would change the pinned route list. Path-parameter 422s compare status only.
        return
    assert (actual.status_code, actual.json()) == (expected.status_code, expected.json())


@pytest.mark.parametrize("case", [
    "pause", "resume", "retry", "archive", "clone", "mark-tested", "set-key",
    "tags", "add", "test-connection", "templates-bulk-import", "entities-bulk-import",
])
def test_post_collision_refusal_is_json(rig, case):
    kwargs = {}
    if case in ("pause", "resume"):
        path = f"/admin/campaigns/{rig.draft}/{case}"
    elif case in ("retry", "archive", "clone", "mark-tested"):
        path = f"/admin/campaigns/9999999/{case}"
    elif case == "set-key":
        path = "/admin/entities/9999999/set-key"
        kwargs["json"] = {"resend_api_key": "re_shadow_valid_test_key"}
    elif case == "tags":
        path = "/admin/tags/delete"
        kwargs["json"] = {"tag": " "}
    elif case == "add":
        path = "/admin/contacts/add-to-campaign"
        kwargs["json"] = {"campaign_id": 9999999, "contact_ids": [rig.contact]}
    elif case == "test-connection":
        path = "/admin/entities/9999999/test-connection"
    else:
        resource = "templates" if case == "templates-bulk-import" else "entities"
        path = f"/admin/{resource}/bulk-import"
        kwargs["files"] = {"file": ("invalid.json", b"not json", "application/json")}
    _same_refusal(rig.client.post(path, **kwargs), rig.reference.post(path, **kwargs))


def test_post_collision_success_is_json(rig):
    def post(path, **kwargs):
        r = rig.client.post(path, **kwargs)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/json")
        return r.json()

    assert post(f"/admin/campaigns/{rig.tested}/pause") == {"paused": True}
    assert post(f"/admin/campaigns/{rig.tested}/resume") == {"resumed": True, "status": "tested"}
    # The tested campaign has a failed recipient; the draft has none.
    assert post(f"/admin/campaigns/{rig.draft}/retry") == {"reset": 0}
    assert post(f"/admin/campaigns/{rig.draft}/mark-tested")["tested"] is True
    assert isinstance(post(f"/admin/campaigns/{rig.tested}/clone")["id"], int)
    assert post(f"/admin/campaigns/{rig.tested}/archive") == {"archived": True}
    assert isinstance(post("/admin/tags/delete", json={"tag": "shadow-tag"})["updated"], int)
    assert post(f"/admin/entities/{rig.entity}/set-key",
                json={"resend_api_key": "re_shadow_test_key"}) == {"key_set": True}


def test_get_export_collisions_are_merged(rig):
    for resource, selected in (("contacts", rig.contact), ("templates", rig.template)):
        for ids in ("", str(selected)):
            path = f"/admin/{resource}/export"
            actual = rig.client.get(path, params={"ids": ids})
            expected = rig.reference.get(path, params={"ids": ids})
            assert (actual.status_code, actual.headers["content-type"], actual.content) == (
                expected.status_code, expected.headers["content-type"], expected.content)
    for ids in ("", str(rig.entity)):
        actual = rig.client.get("/admin/entities/export", params={"ids": ids})
        expected = rig.api.export_entities(None)
        assert (actual.status_code, actual.headers["content-type"], actual.content) == (
            expected.status_code, expected.headers["content-type"], expected.body)
        # The API's earlier /entities/{entity_id} route shadows its own export.
        assert rig.reference.get("/admin/entities/export", params={"ids": ids}).status_code == 422


def test_get_page_collisions_are_html(rig):
    for page in ("contacts", "templates", "campaigns", "suppression", "entities",
                 "operators", "schedule", "tags", "seeds"):
        r = rig.client.get(f"/admin/{page}")
        assert r.status_code == 200, (page, r.text)
        assert r.headers["content-type"].startswith("text/html"), page


def test_collision_inventory_is_exact(rig):
    def inventory(router):
        return {(method, re.sub(r"\{[^}]+\}", "{}", route.path))
                for route in router.routes for method in route.methods}

    expected = {
        ("POST", "/admin/campaigns/{}/pause"),
        ("POST", "/admin/campaigns/{}/resume"),
        ("POST", "/admin/campaigns/{}/retry"),
        ("POST", "/admin/campaigns/{}/archive"),
        ("POST", "/admin/campaigns/{}/clone"),
        ("POST", "/admin/campaigns/{}/mark-tested"),
        ("POST", "/admin/contacts/add-to-campaign"),
        ("POST", "/admin/tags/delete"),
        ("POST", "/admin/templates/bulk-import"),
        ("POST", "/admin/entities/bulk-import"),
        ("POST", "/admin/entities/{}/test-connection"),
        ("POST", "/admin/entities/{}/set-key"),
        ("GET", "/admin/contacts"), ("GET", "/admin/templates"),
        ("GET", "/admin/campaigns"), ("GET", "/admin/suppression"),
        ("GET", "/admin/entities"), ("GET", "/admin/operators"),
        ("GET", "/admin/schedule"), ("GET", "/admin/tags"), ("GET", "/admin/seeds"),
        ("GET", "/admin/contacts/export"), ("GET", "/admin/templates/export"),
        ("GET", "/admin/entities/export"),
    }
    assert inventory(rig.admin_ui.router) & inventory(rig.api.router) == expected


def test_pages_surface_refusals(rig):
    def page(path):
        r = rig.client.get(path)
        assert r.status_code == 200, r.text
        return r.text

    campaigns = page("/admin/campaigns")
    camp_action = campaigns.split("function campAction(id, verb){", 1)[1].split(
        "r.text().then(function(t){\n", 1)[0]
    assert "typeof e.detail==='string'" in camp_action
    assert 'action="/admin/campaigns/\'+_testCampId+\'/mark-tested"' not in campaigns
    assert "&#10003; Mark as tested" in campaigns

    analytics = page(f"/admin/campaigns/{rig.tested}/analytics")
    assert 'onclick="retryFailed(' in analytics
    assert f'action="/admin/campaigns/{rig.tested}/retry"' not in analytics
    retry = analytics.split("function retryFailed(cid){", 1)[1].split("</script>", 1)[0]
    assert "if(r.ok)" in retry and "location.reload()" in retry
    assert "typeof e.detail==='string'" in retry

    entities = page("/admin/entities")
    assert 'onsubmit="return saveKey(event)"' in entities
    assert 'onclick="saveKey(event)"' in entities
    assert "getElementById('key-form').submit()" not in entities
    save_key = entities.split("async function saveKey(ev){", 1)[1].split("</script>", 1)[0]
    statements = save_key.lstrip()
    assert statements.startswith("ev.preventDefault();\n")
    assert statements.splitlines()[1].strip() == (
        "if(!document.getElementById('key-form').reportValidity())return false;")
    assert "typeof e.detail==='string'" in save_key

    contacts = page("/admin/contacts")
    add = contacts.split("fetch('/admin/contacts/add-to-campaign'", 1)[1].split("alert('Added '", 1)[0]
    assert "if(!r.ok)" in add
    assert "typeof e.detail==='string'" in add
    tags = page("/admin/tags")
    delete = tags.split("fetch('/admin/tags/delete'", 1)[1].split("updated+=", 1)[0]
    assert "if(!r.ok)" in delete
    assert "typeof e.detail==='string'" in delete
    refusal = delete.split("if(!r.ok)", 1)[1]
    assert refusal.index("alert(m||'Error '+r.status)") < refusal.index("setResult(") < refusal.index("return;")
    assert "Removed '+i+' tag(s) from '+updated+' contacts." in refusal


def test_pause_resume_volume_and_bounds(rig):
    for press in range(50):
        verb = "pause" if press % 2 == 0 else "resume"
        r = rig.client.post(f"/admin/campaigns/{rig.tested}/{verb}")
        assert r.status_code in (200, 400), (press, r.text)
        assert r.status_code != 303
        assert r.headers["content-type"].startswith("application/json")
        expected = {"paused": True} if verb == "pause" else {"resumed": True, "status": "tested"}
        assert r.status_code == 200 and r.json() == expected, (press, r.text)
    assert rig.api.get_campaign(rig.tested)["campaign"]["status"] == "tested"
    for cid in (0, -1, 2**63 - 1, "abc"):
        path = f"/admin/campaigns/{cid}/pause"
        # Path-parameter 422s deliberately compare status only: wrapper/API
        # parameter names differ, and renaming the wrapper would change the pinned route list.
        _same_refusal(rig.client.post(path), rig.reference.post(path))


def test_set_key_form_422_does_not_echo_key(rig):
    value = "re_shadow_distinctive_form_value_71bdc9"
    r = rig.client.post(f"/admin/entities/{rig.entity}/set-key",
                        data={"resend_api_key": value})
    assert r.status_code == 422
    assert r.json() == {"detail": "request body must be JSON with resend_api_key"}
    assert value not in r.text


@pytest.mark.parametrize("blank", ["", " \t\n "], ids=["empty", "whitespace"])
def test_set_key_blank_preserves_stored_key(rig, blank):
    from sqlalchemy import select
    from mailman.terminal.models import Entity

    path = f"/admin/entities/{rig.entity}/set-key"
    r = rig.client.post(path, json={"resend_api_key": "re_shadow_preserved_key"})
    assert r.status_code == 200 and r.json() == {"key_set": True}
    with rig.db.session() as s:
        before = s.execute(select(Entity.resend_key_enc).where(Entity.id == rig.entity)).scalar_one()
    r = rig.client.post(path, json={"resend_api_key": blank})
    assert r.status_code == 422
    assert r.json() == {"detail": "key must not be empty"}
    with rig.db.session() as s:
        after = s.execute(select(Entity.resend_key_enc).where(Entity.id == rig.entity)).scalar_one()
    assert before is not None and after == before


def test_tags_delete_json_updates_contacts(rig):
    r = rig.client.post("/admin/tags/delete", json={"tag": "shadow-tag"})
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["updated"], int)
    assert r.json()["updated"] == 1


def test_add_to_missing_campaign_matches_reference(rig):
    body = {"campaign_id": 9999999, "contact_ids": [rig.contact]}
    actual = rig.client.post("/admin/contacts/add-to-campaign", json=body)
    expected = rig.reference.post("/admin/contacts/add-to-campaign", json=body)
    assert actual.status_code == expected.status_code == 404
    assert actual.json() == expected.json()


# Literal sorted main.app route list derived from decorators,
# including FastAPI's four GET/HEAD documentation routes and duplicate entries.
DELEGATED_ROUTES = [
    "DELETE /admin/campaigns/{campaign_id}/stages/{stage_id}",
    "DELETE /admin/contacts/{contact_id}",
    "DELETE /admin/entities/{entity_id}",
    "DELETE /admin/entities/{entity_id}/key",
    "DELETE /admin/operators/{op_id}",
    "DELETE /admin/seeds/{seed_id}",
    "DELETE /admin/settings/api/{key}",
    "DELETE /admin/templates/{template_id}",
    "GET /admin",
    "GET /admin/audit",
    "GET /admin/audit-log",
    "GET /admin/auth/login",
    "GET /admin/backup/run",
    "GET /admin/campaigns",
    "GET /admin/campaigns",
    "GET /admin/campaigns/{campaign_id}",
    "GET /admin/campaigns/{campaign_id}/analytics/data",
    "GET /admin/campaigns/{campaign_id}/batches",
    "GET /admin/campaigns/{campaign_id}/stages",
    "GET /admin/campaigns/{campaign_id}/test-send-chain",
    "GET /admin/campaigns/{cid}/analytics",
    "GET /admin/campaigns/{cid}/recipients",
    "GET /admin/contacts",
    "GET /admin/contacts",
    "GET /admin/contacts/count",
    "GET /admin/contacts/export",
    "GET /admin/contacts/export",
    "GET /admin/contacts/json",
    "GET /admin/docs",
    "GET /admin/drift",
    "GET /admin/entities",
    "GET /admin/entities",
    "GET /admin/entities/export",
    "GET /admin/entities/export",
    "GET /admin/entities/{entity_id}",
    "GET /admin/operators",
    "GET /admin/operators",
    "GET /admin/quicksend",
    "GET /admin/schedule",
    "GET /admin/schedule",
    "GET /admin/seeds",
    "GET /admin/seeds",
    "GET /admin/settings",
    "GET /admin/settings/api",
    "GET /admin/settings/api/{key}",
    "GET /admin/settings/api/{key}/reveal",
    "GET /admin/settings/auth",
    "GET /admin/settings/brand",
    "GET /admin/settings/cloud",
    "GET /admin/settings/reveal/{key}",
    "GET /admin/settings/secrets",
    "GET /admin/settings/sending",
    "GET /admin/settings/tuning",
    "GET /admin/settings/webhooks",
    "GET /admin/suppression",
    "GET /admin/suppression",
    "GET /admin/suppression/csv",
    "GET /admin/system/status",
    "GET /admin/tags",
    "GET /admin/tags",
    "GET /admin/templates",
    "GET /admin/templates",
    "GET /admin/templates/export",
    "GET /admin/templates/export",
    "GET /admin/templates/{tid}/preview",
    "GET /admin/templates/{tid}/preview/raw",
    "GET /docs",
    "GET /docs/oauth2-redirect",
    "GET /favicon.ico",
    "GET /healthz",
    "GET /openapi.json",
    "GET /redoc",
    "GET /sync/status",
    "HEAD /docs",
    "HEAD /docs/oauth2-redirect",
    "HEAD /openapi.json",
    "HEAD /redoc",
    "PATCH /admin/campaigns/{campaign_id}",
    "PATCH /admin/campaigns/{campaign_id}/stages/{stage_id}",
    "PATCH /admin/contacts/{contact_id}",
    "PATCH /admin/entities/{entity_id}",
    "PATCH /admin/templates/{template_id}",
    "POST /admin/auth/login",
    "POST /admin/auth/logout",
    "POST /admin/batches/{batch_id}/placement",
    "POST /admin/campaigns",
    "POST /admin/campaigns/create",
    "POST /admin/campaigns/{campaign_id}/archive",
    "POST /admin/campaigns/{campaign_id}/batches/{batch_no}/approve",
    "POST /admin/campaigns/{campaign_id}/cancel",
    "POST /admin/campaigns/{campaign_id}/clone",
    "POST /admin/campaigns/{campaign_id}/launch",
    "POST /admin/campaigns/{campaign_id}/mark-tested",
    "POST /admin/campaigns/{campaign_id}/pause",
    "POST /admin/campaigns/{campaign_id}/recipients",
    "POST /admin/campaigns/{campaign_id}/relaunch",
    "POST /admin/campaigns/{campaign_id}/resume",
    "POST /admin/campaigns/{campaign_id}/retry",
    "POST /admin/campaigns/{campaign_id}/schedule",
    "POST /admin/campaigns/{campaign_id}/stages",
    "POST /admin/campaigns/{campaign_id}/test-send",
    "POST /admin/campaigns/{cid}/archive",
    "POST /admin/campaigns/{cid}/clone",
    "POST /admin/campaigns/{cid}/edit",
    "POST /admin/campaigns/{cid}/mark-tested",
    "POST /admin/campaigns/{cid}/pause",
    "POST /admin/campaigns/{cid}/recipients/by-tag",
    "POST /admin/campaigns/{cid}/resume",
    "POST /admin/campaigns/{cid}/retry",
    "POST /admin/campaigns/{cid}/stages/add",
    "POST /admin/campaigns/{cid}/stages/{stage_id}/delete",
    "POST /admin/contacts",
    "POST /admin/contacts/add-to-campaign",
    "POST /admin/contacts/add-to-campaign",
    "POST /admin/contacts/bulk-delete",
    "POST /admin/contacts/create",
    "POST /admin/contacts/csv",
    "POST /admin/contacts/csv/chunk",
    "POST /admin/contacts/{cid}/delete",
    "POST /admin/contacts/{contact_id}/edit",
    "POST /admin/entities",
    "POST /admin/entities/bulk-import",
    "POST /admin/entities/bulk-import",
    "POST /admin/entities/create",
    "POST /admin/entities/{cid}/delete",
    "POST /admin/entities/{cid}/set-key",
    "POST /admin/entities/{cid}/test-connection",
    "POST /admin/entities/{cid}/update",
    "POST /admin/entities/{entity_id}/set-key",
    "POST /admin/entities/{entity_id}/test-connection",
    "POST /admin/operators",
    "POST /admin/operators/create",
    "POST /admin/operators/{op_id}/delete",
    "POST /admin/operators/{op_id}/toggle",
    "POST /admin/operators/{op_id}/toggle-active",
    "POST /admin/quicksend",
    "POST /admin/seeds",
    "POST /admin/seeds/{seed_id}/toggle",
    "POST /admin/settings/api",
    "POST /admin/settings/auth/password",
    "POST /admin/settings/auth/rotate-jwt",
    "POST /admin/settings/cloud/save",
    "POST /admin/settings/{key}/save",
    "POST /admin/suppression",
    "POST /admin/tags/add-to-filtered",
    "POST /admin/tags/delete",
    "POST /admin/tags/delete",
    "POST /admin/tags/rename",
    "POST /admin/templates",
    "POST /admin/templates/bulk-import",
    "POST /admin/templates/bulk-import",
    "POST /admin/templates/create",
    "POST /admin/templates/import",
    "POST /admin/templates/{tid}/delete",
    "POST /admin/templates/{tid}/preview/raw",
    "POST /admin/templates/{tid}/save",
]


def test_main_route_inventory(rig):
    actual = sorted(f"{method} {route.path}" for route in rig.main.app.routes
                    for method in route.methods)
    assert actual == DELEGATED_ROUTES
