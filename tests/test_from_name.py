"""The sender header retains the display name and keeps it on one header line."""
import asyncio
import base64
import json
from email.utils import formataddr
from types import SimpleNamespace

import pytest


def test_from_header_named():
    from mailman.terminal.launch import _from_header
    assert _from_header("Demo Co", "hello@demo.example") == "Demo Co <hello@demo.example>"


def test_from_header_empty():
    from mailman.terminal.launch import _from_header
    for name in ("", None, "  \r\n "):
        assert _from_header(name, "hello@demo.example") == "hello@demo.example"


def test_from_header_collapses_header_breaks():
    from mailman.terminal.launch import _from_header
    header = _from_header("Evil\r\nBcc: x@y", "hello@demo.example")
    assert header == formataddr(("Evil Bcc: x@y", "hello@demo.example"))
    assert "\r" not in header and "\n" not in header


def test_from_header_quotes_special_characters():
    from mailman.terminal.launch import _from_header
    for name in ("Demo, Co", 'Demo "Co"'):
        header = _from_header(name, "hello@demo.example")
        assert header == formataddr((name, "hello@demo.example"))
        assert header.startswith('"') and header.endswith('" <hello@demo.example>')
        if '"' in name:
            assert '\\"' in header


def test_from_header_encodes_non_ascii():
    from mailman.terminal.launch import _from_header
    header = _from_header("D\u00e9mo Co", "hello@demo.example")
    assert header.startswith("=?utf-8?") and "?=" in header
    assert header.endswith(" <hello@demo.example>")
    assert header.isascii()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    from mailman.terminal import auth, db, encryption, launch, main, routes_admin, settings

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.sqlite")
    keys = tmp_path / "keys"; keys.mkdir()
    monkeypatch.setattr(encryption, "KEYS_DIR", keys)
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    db.init()
    main.app.dependency_overrides[auth.require_session] = lambda: {"sub": "retry@example.com", "role": "admin"}
    c = TestClient(main.app)
    (keys / "cloud_bearer.txt").write_text("tok")
    settings.set("email_footer_address", "1 Example Street")

    def _post(c, path, body):
        r = c.post(path, json=body)
        assert r.status_code in (200, 201), (path, r.status_code, r.text[:300])
        return r.json()

    eid = _post(c, "/admin/entities", {"name": "Company Name", "domain": "demo.example", "from_name": "Demo Co",
                "from_email": "hello@demo.example", "footer_address": "1 Example Street",
                "support_email": "support@demo.example", "public_host": "localhost"})["id"]
    _post(c, f"/admin/entities/{eid}/set-key", {"resend_api_key": "re_x1"})

    k = encryption.mint_k_temp()
    pushed = []
    async def _fixed_k_temp(bearer):
        return k
    async def _push(packs, bearer):
        pushed.extend(packs)
        return {"accepted": len(packs), "submitted": 0, "enqueued": 0, "deferred": 0}
    monkeypatch.setattr(launch, "_ensure_k_temp", _fixed_k_temp)
    monkeypatch.setattr(launch, "_push_packs", _push)

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

    def _sql(q, args=()):
        con = sqlite3.connect(str(tmp_path / "state.sqlite"))
        try:
            rows = con.execute(q, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    yield SimpleNamespace(c=c, eid=eid, campaign=_campaign, sql=_sql, launch=launch,
                          encryption=encryption, pushed=pushed, k=k)
    main.app.dependency_overrides.clear()


@pytest.mark.parametrize("name, expected", [("Demo Co", "Demo Co <hello@demo.example>"),
                                           ("", "Company Name <hello@demo.example>")])
def test_launch_pack_from_name(rig, name, expected):
    rig.sql("UPDATE entities SET from_name=? WHERE id=?", (name, rig.eid))
    cid = rig.campaign(rig.c, 1, "sender")
    asyncio.run(rig.launch.launch_campaign(cid))
    assert len(rig.pushed) == 1
    plain = json.loads(rig.encryption.decrypt_with_temp(
        base64.b64decode(rig.pushed[0]["encrypted_blob"]), rig.k))
    assert plain["from"] == expected
    assert plain["from"] != "hello@demo.example"
