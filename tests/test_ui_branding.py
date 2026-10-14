"""Favicon, tags script, sync card,
badge removal and warning, v4 labels and scripts/v3 copy, .gitattributes. Rendered through main.app on a
db.init() DB seeded through the app's own admin API."""
from __future__ import annotations
import asyncio, fnmatch, html, json, re, sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
ICON = (ROOT / "docs" / "img" / "icon.png").read_bytes()
SUB = "card-tester@example.com"
FAV = '<link rel="icon" type="image/png" href="/favicon.ico">'
BADGE = "ENCRYPTED END-TO-END"
WARNING = ("This template still contains the old 'ENCRYPTED END-TO-END' badge text, "
           "which is not accurate. Remove it before sending.")

@pytest.fixture
def ui(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from mailman.terminal import auth, db, encryption, main
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.sqlite")
    keys = tmp_path / "keys"; keys.mkdir()
    monkeypatch.setattr(encryption, "KEYS_DIR", keys)
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    db.init()
    main.app.dependency_overrides[auth.require_session] = lambda: {"sub": SUB, "role": "admin"}
    c = TestClient(main.app)
    def post(path, body):
        r = c.post(path, json=body)
        assert r.status_code in (200, 201), (path, r.status_code, r.text[:300])
        return r.json()
    for e, n, t in [("ann@demo.example", "Ann Example", "vip|newsletter"), ("bob@demo.example", "Bob Sample", "newsletter"),
                    ("cy@demo.example", "Cy Demo", "trial")]:
        post("/admin/contacts", {"email": e, "name": n, "tags": t, "consent_source": "demo"})
    t_badge = post("/admin/templates", {"name": "Badge template", "subject": "Hi {{firstName}}", "from_name": "Demo Co",
              "html_body": f"<div class='badge'>{BADGE}</div><p>Hi {{{{name}}}}</p><p>{{{{companyName}}}}</p>", "text_body": "Hi"})["id"]
    t_clean = post("/admin/templates", {"name": "Clean template", "subject": "Hello", "from_name": "Demo Co",
              "html_body": "<p>Hello {{name}}</p>", "text_body": "Hello"})["id"]
    eid = post("/admin/entities", {"name": "Demo Co", "domain": "demo.example", "from_name": "Demo Co",
              "from_email": "hello@demo.example", "footer_address": "1 Example Street",
              "support_email": "support@demo.example", "public_host": "localhost"})["id"]
    c_badge = post("/admin/campaigns", {"name": "Badge campaign", "template_id": t_badge, "entity_id": eid})["id"]
    c_clean = post("/admin/campaigns", {"name": "Clean campaign", "template_id": t_clean, "entity_id": eid})["id"]
    post(f"/admin/campaigns/{c_badge}/recipients", {"by_tag": "newsletter"})
    ids = dict(t_badge=t_badge, t_clean=t_clean, c_badge=c_badge, c_clean=c_clean)
    yield c, ids, tmp_path
    main.app.dependency_overrides.clear()

def routes(ids):
    return ["/admin/auth/login", "/admin", "/admin/contacts", "/admin/templates",
            f"/admin/templates/{ids['t_badge']}/preview", f"/admin/templates/{ids['t_clean']}/preview",
            "/admin/campaigns", f"/admin/campaigns/{ids['c_badge']}/recipients",
            f"/admin/campaigns/{ids['c_badge']}/analytics", "/admin/schedule", "/admin/quicksend", "/admin/tags",
            "/admin/audit", "/admin/entities", "/admin/suppression", "/admin/drift", "/admin/operators",
            "/admin/seeds", "/admin/docs", "/admin/settings/sending", "/admin/backup/run"]

def page(c, path):
    r = c.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), (path, r.status_code)
    return r.text

_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KW = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw",
             "instanceof", "yield", "await"}
_PAIRS = {")": "(", "]": "[", "}": "{"}

def inline_scripts(doc):
    return [m["body"] for m in _SCRIPT_RE.finditer(doc) if "src=" not in m["attrs"].lower()]

def js_lex_errors(src):
    """Lexical check: strings, comments and regex literals terminate; brackets nest; no two string
    literals side by side on one line. No page uses template literals; they are
    reported so this checker is extended before one ships."""
    errs, stack = [], []
    i, n, line = 0, len(src), 1
    kind, word, prev_line = None, "", 0
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1; i += 1; continue
        if c in " \t\r\f\v":
            i += 1; continue
        if src.startswith("//", i):
            j = src.find("\n", i); i = n if j < 0 else j; continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                errs.append(f"line {line}: unterminated comment"); break
            line += src.count("\n", i, j); i = j + 2; continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            if j >= n or src[j] != c:
                errs.append(f"line {line}: unterminated string"); break
            if kind == "str" and prev_line == line:
                errs.append(f"line {line}: adjacent string literals")
            kind, word, prev_line, i = "str", "", line, j + 1; continue
        if c == "`":
            errs.append(f"line {line}: template literal (unsupported by this checker)"); break
        if c == "/" and (kind is None or (kind == "punct" and word in _REGEX_AFTER)
                         or (kind == "word" and word in _REGEX_KW)):
            j, in_class = i + 1, False
            while j < n and src[j] != "\n":
                ch = src[j]
                if ch == "\\":
                    j += 2; continue
                if ch == "[": in_class = True
                elif ch == "]": in_class = False
                elif ch == "/" and not in_class: break
                j += 1
            if j >= n or src[j] != "/":
                errs.append(f"line {line}: unterminated regex"); break
            j += 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            kind, word, prev_line, i = "regex", "", line, j; continue
        if c.isalpha() or c in "_$":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            kind, word, prev_line, i = "word", src[i:j], line, j; continue
        if c.isdigit():
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "._"):
                j += 1
            kind, word, prev_line, i = "num", "", line, j; continue
        if c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if not stack or stack[-1][0] != _PAIRS[c]:
                errs.append(f"line {line}: unmatched {c!r}"); break
            stack.pop()
        kind, word, prev_line, i = "punct", c, line, i + 1
    if not errs and stack:
        errs.append(f"line {stack[-1][1]}: unclosed {stack[-1][0]!r}")
    return errs

BAD = r"""var h='<b onclick="removeTagFromContact('+c.id+',''+c.email.replace(/'/g,"\'")+"')>x</b>";"""
GOOD = r"""var h='<b onclick="removeTagFromContact('+c.id+',\''+c.email.replace(/'/g,"\\'")+'\')">x</b>';"""

def _ga_rules():
    rules = []
    for raw in (ROOT / ".gitattributes").read_text().splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            pat, *attrs = s.split()
            rules.append((pat, attrs))
    return rules

def _ga_match(rel, pat):
    if "/" not in pat:
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pat)
    ps, rs = pat.strip("/").split("/"), rel.split("/")
    return len(ps) == len(rs) and all(fnmatch.fnmatchcase(r, p) for r, p in zip(rs, ps))

def _ga_attrs(rel, rules):
    got = []
    for pat, attrs in rules:
        if _ga_match(rel, pat):
            got = attrs
    return got

def _executed_files():
    out = []
    for base in ("deploy", "scripts"):
        for p in sorted((ROOT / base).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                rel = p.relative_to(ROOT).as_posix()
                if rel.endswith((".sh", ".Dockerfile")) or p.read_bytes()[:2] == b"#!":
                    out.append(rel)
    return out

def _sql(tmp, q, args=()):
    con = sqlite3.connect(str(tmp / "state.sqlite"))
    try:
        rows = con.execute(q, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()

# favicon

def test_favicon_route_serves_icon_bytes(ui):
    c, ids, tmp = ui
    from mailman.terminal import brand
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == ICON
    assert brand.ICON_PNG == ICON

def test_every_html_page_links_favicon(ui):
    c, ids, tmp = ui
    missing = [p for p in routes(ids) if FAV not in page(c, p)]
    assert missing == []

def test_embedded_icons_match_docs_icon(ui):
    import base64
    c, ids, tmp = ui
    for p in ("/admin/auth/login", "/admin"):
        embeds = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", page(c, p))
        assert embeds, p
        assert all(base64.b64decode(b) == ICON for b in embeds), p

# tags script

def test_js_lexer_flags_c23daf5_shape():
    assert any("adjacent string literals" in e for e in js_lex_errors(BAD))
    assert js_lex_errors(GOOD) == []

def test_admin_inline_scripts_lex_clean(ui):
    c, ids, tmp = ui
    bad = {}
    for p in routes(ids):
        errs = [e for s in inline_scripts(page(c, p)) for e in js_lex_errors(s)]
        if errs:
            bad[p] = errs
    assert bad == {}

def test_tags_onclick_builders_fixed(ui):
    c, ids, tmp = ui
    body = page(c, "/admin/tags")
    assert r"""function jsAttrQ(s){return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/&/g,'&amp;').replace(/"/g,'&quot;')}""" in body
    assert r"""onclick="removeTagFromContact('+c.id+',\''+jsAttrQ(c.email)+'\')">X</button>'""" in body
    assert r"""onclick="addTagToContact('+c.id+',\''+jsAttrQ(c.email)+'\',\''+jsAttrQ(c.tags||'')+'\')">+ Add</button>'""" in body

# sync card

def test_overview_sync_card_not_handshaken(ui):
    c, ids, tmp = ui
    from mailman.terminal import admin_ui, sync_client
    body = page(c, "/admin")
    card = admin_ui._sync_card_html()
    assert card in body
    assert '<span class="pill crit"><span class="dot"></span>handshake pending</span>' in card
    assert ">healthy<" not in body and "WG actor" not in body
    assert "Sync actor" in card and html.escape(sync_client.TERMINAL_ACTOR) in card
    assert SUB not in card
    assert '<span class="muted">Last pull</span><span class="mono cell-dim">never</span>' in card

def _pill(card, pill, label):
    return f'<span class="pill {pill}"><span class="dot"></span>{label}</span>' in card

def _set_last_pull(tmp, value):
    _sql(tmp, "INSERT OR REPLACE INTO sync_state(key,value,updated_at) VALUES ('last_pull_ok_at',?,datetime('now'))", (value,))

def test_overview_sync_card_states(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import admin_ui, sync_client
    NOW = datetime(2026, 10, 11, 12, 0, 0)
    (tmp / "keys" / "cloud_bearer.txt").write_text("tok")
    assert _pill(admin_ui._sync_card_html(now=NOW), "warn", "no pull yet")
    _set_last_pull(tmp, (NOW - timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S"))
    assert _pill(admin_ui._sync_card_html(now=NOW), "ok", "pulling")
    _set_last_pull(tmp, (NOW - timedelta(seconds=301)).strftime("%Y-%m-%d %H:%M:%S"))
    assert _pill(admin_ui._sync_card_html(now=NOW), "warn", "pull stale")
    _set_last_pull(tmp, "garbage")
    card = admin_ui._sync_card_html(now=NOW)
    assert _pill(card, "warn", "pull stale") and "garbage UTC" in card
    _sql(tmp, "INSERT INTO outbox(table_name,row_id,revision,actor,modified_at,key_id,pushed_at) "
              "VALUES ('contacts','1',1,'t','2026-10-11 11:00:00','k','2026-10-11 11:59:00')")
    last_push = _sql(tmp, "SELECT MAX(pushed_at) FROM outbox")[0][0]
    pending = _sql(tmp, "SELECT COUNT(*) FROM outbox WHERE pushed_at IS NULL")[0][0]
    card = admin_ui._sync_card_html(now=NOW)
    assert f'<span class="muted">Last push</span><span class="mono cell-dim">{last_push} UTC</span>' in card
    assert f'<span class="muted">Outbox pending</span><span class="mono cell-dim">{pending}</span>' in card
    monkeypatch.setattr(sync_client, "TERMINAL_ACTOR", "<b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in admin_ui._sync_card_html(now=NOW)
    _set_last_pull(tmp, "2026-10-11T11:59:00+00:00")
    card = admin_ui._sync_card_html(now=NOW)
    assert _pill(card, "warn", "pull stale") or _pill(card, "ok", "pulling")

# badge

def test_badge_insert_removed(ui):
    c, ids, tmp = ui
    for p in ("/admin/templates", f"/admin/templates/{ids['t_badge']}/preview", f"/admin/templates/{ids['t_clean']}/preview"):
        body = page(c, p)
        for s in ("insertBadge", "BADGE_HTML", "Insert security badge"):
            assert s not in body, (p, s)
    for p in ("/admin/docs", "/admin/auth/login"):
        assert BADGE not in page(c, p), p

def test_old_badge_template_warns(ui):
    c, ids, tmp = ui
    callout = f'<div class="callout warn" style="margin-bottom:16px">{html.escape(WARNING)}</div>'
    assert callout in page(c, f"/admin/templates/{ids['t_badge']}/preview")
    assert html.escape(WARNING) not in page(c, f"/admin/templates/{ids['t_clean']}/preview")
    body = page(c, "/admin/campaigns")
    assert f'data-old-badge="1" onclick="launchCampaign({ids["c_badge"]},this,false)"' in body
    assert f'data-old-badge="0" onclick="launchCampaign({ids["c_clean"]},this,false)"' in body
    assert "var OLD_BADGE_WARNING=" + json.dumps(WARNING) in body
    assert r"msg=OLD_BADGE_WARNING+'\n\n'+msg" in body
    _sql(tmp, "UPDATE campaigns SET status='sent' WHERE id=?", (ids["c_badge"],))
    body = page(c, "/admin/campaigns")
    assert f'data-old-badge="1" onclick="launchCampaign({ids["c_badge"]},this,true)">Re-launch</button>' in body

def test_demo_template_has_no_badge():
    d = json.loads((ROOT / "docs" / "examples" / "demo-template.json").read_text(encoding="utf-8"))
    for k in ("subject", "html_body"):
        for s in (BADGE, "\u2726", "\U0001f44b"):
            assert s not in d[k], (k, s)
    assert "{{firstName}}" in d["subject"]

# v4 labels and scripts/v3 copy

def test_v4_labels(ui):
    c, ids, tmp = ui
    body = page(c, "/admin")
    assert 'MailMan<span class="v">v4</span>' in body and '<span class="v">v3</span>' not in body
    login = page(c, "/admin/auth/login")
    assert '<div class="dim">MailMan v4 terminal</div>' in login and "ep-v3" not in login

def test_no_bin_v3_in_rendered_pages(ui):
    c, ids, tmp = ui
    assert [p for p in routes(ids) if "bin/v3" in page(c, p)] == []
    assert "scripts/v3 init-handshake" in page(c, "/admin/entities")
    assert "scripts/v3 backup" in page(c, "/admin/backup/run")

def test_handshake_errors_name_scripts_v3(ui):
    from mailman.terminal import encryption, launch
    with pytest.raises(encryption.KeyBundleNotReady) as e1:
        encryption.KeyBundle.load("x")
    with pytest.raises(launch.LaunchError) as e2:
        asyncio.run(launch.launch_campaign(1))
    for e in (e1, e2):
        assert "scripts/v3 init-handshake" in str(e.value) and "bin/v3" not in str(e.value)

def test_functional_v3_identifiers_kept():
    from mailman.terminal import auth
    assert auth.SESSION_COOKIE == "v3_session"

# .gitattributes

def test_gitattributes_pins_executed_files_lf():
    rules = _ga_rules()
    executed = _executed_files()
    assert "deploy/entrypoint.sh" in executed and "scripts/v3" in executed
    for rel in executed:
        attrs = _ga_attrs(rel, rules)
        assert "text" in attrs and "eol=lf" in attrs, (rel, attrs)
    assert all(pat != "*" for pat, _ in rules)
    assert not _ga_match("scripts/sub/tool.py", "scripts/*.py")
    assert _ga_match("scripts/_backup.py", "scripts/*.py")

# stress

def test_stress_favicon_and_tags(ui):
    c, ids, tmp = ui
    from mailman.terminal import admin_ui
    for _ in range(200):
        r = c.get("/favicon.ico")
        assert r.status_code == 200 and r.content == ICON
    first = None
    for _ in range(50):
        scripts = inline_scripts(page(c, "/admin/tags"))
        assert all(js_lex_errors(s) == [] for s in scripts)
        first = scripts if first is None else first
        assert scripts == first
    (tmp / "keys" / "cloud_bearer.txt").write_text("tok")
    T = datetime(2026, 10, 11, 12, 0, 0)
    _set_last_pull(tmp, T.strftime("%Y-%m-%d %H:%M:%S"))
    for age in range(0, 601):
        card = admin_ui._sync_card_html(now=T + timedelta(seconds=age))
        assert _pill(card, "ok", "pulling") == (age <= 300), age

# {{companyName}} and the send paths

RENDER_VARS = {"name", "email", "firstName", "companyName", "first_name", "full_name", "fullName"}
COMPLIANCE_VARS = {"unsubscribeUrl", "erasureUrl", "unsubscribe_url", "unsubUrl", "erasure_url", "deleteUrl"}
ALL_VARS = RENDER_VARS | COMPLIANCE_VARS
UNFILLED_PREVIEW_ERROR = "unfilled template variables: {{previewText}} - use only the variables listed on the Templates page"

class _Stop(Exception):
    pass

def _capture(monkeypatch, tmp):
    from mailman.terminal import encryption, launch, routes_admin
    k = encryption.mint_k_temp()
    packs = []
    async def _k(bearer):
        return k
    async def _push(p, bearer):
        packs.extend(p)
        raise _Stop()
    monkeypatch.setattr(launch, "_ensure_k_temp", _k)
    monkeypatch.setattr(routes_admin, "get_entity_credentials", lambda eid: {
        "resend_key": "re_x1", "from_name": "Demo Co", "from_email": "hello@demo.example", "domain": "demo.example"})
    monkeypatch.setattr(launch, "_push_packs", _push)
    (tmp / "keys" / "cloud_bearer.txt").write_text("tok")
    return packs, k

def _open(p, k):
    import base64
    from mailman.terminal import encryption
    return json.loads(encryption.decrypt_with_temp(base64.b64decode(p["encrypted_blob"]), k))

def _eid(tmp):
    return _sql(tmp, "SELECT id FROM entities WHERE name='Demo Co'")[0][0]

def _mark_tested(cid):
    from mailman.terminal import routes_admin
    routes_admin.mark_tested(cid)

def _campaign(c, eid, html_body, tag):
    t = c.post("/admin/templates", json={"name": f"T {tag}", "subject": "Hi", "from_name": "Demo Co",
                                         "html_body": html_body, "text_body": "x"}).json()["id"]
    cid = c.post("/admin/campaigns", json={"name": f"C {tag}", "template_id": t, "entity_id": eid}).json()["id"]
    c.post(f"/admin/campaigns/{cid}/recipients", json={"by_tag": tag})
    return cid

def _fake_httpx(monkeypatch):
    import httpx
    sent = []
    class _R:
        status_code, text = 200, ""
        def json(self):
            return {"id": "m-1"}
    def _post(url, **kw):
        sent.append(kw["json"]); return _R()
    monkeypatch.setattr(httpx, "post", _post)
    return sent

def test_advertised_variables_are_known(ui):
    c, ids, tmp = ui
    docs = [page(c, p) for p in ("/admin/templates", f"/admin/templates/{ids['t_badge']}/preview",
                                 f"/admin/templates/{ids['t_clean']}/preview", "/admin/docs", "/admin/contacts")]
    docs.append((ROOT / "docs" / "examples" / "demo-template.json").read_text(encoding="utf-8"))
    found = {m for d in docs for m in re.findall(r"\{\{([A-Za-z_]+)\}\}", d)}
    assert found - ALL_VARS == set()

def test_send_path_leaves_no_tokens(ui):
    c, ids, tmp = ui
    from mailman.terminal import encryption, launch
    subj = " ".join("{{%s}}" % v for v in sorted(RENDER_VARS))
    toks = " ".join("{{%s}}" % v for v in sorted(ALL_VARS))
    tmpl = {"subject": subj, "html_body": f"<p>{toks}</p>", "text_body": toks}
    k = encryption.mint_k_temp()
    for kind in ("promotional", "transactional"):
        ctx = launch._SendCtx(campaign_id=ids["c_badge"], camp={"kind": kind}, tmpl=tmpl, public_host="localhost",
                              resend_key="re_x1", email_from="hello@demo.example", email_from_name="Demo Co",
                              k_temp=k, key_id="K_temp_x", bearer="tok", company_name="Demo Co")
        for r, seed in (({"email": "ann@demo.example", "name": "Ann Example", "contact_id": 1}, False),
                        ({"email": "seed@demo.example", "name": "", "contact_id": None}, True)):
            p, _ = launch._build_pack(ctx, r, "2026-10-11T12:00:00Z", human_send=False, is_seed=seed)
            m = _open(p, k)
            assert all("{{" not in m[f] for f in ("subject", "html", "text")), (kind, seed)
            assert "Demo Co" in m["html"], (kind, seed)
    ctx.tmpl = dict(tmpl, subject=subj + " {{unsubscribeUrl}}")
    with pytest.raises(launch.LaunchError) as ei:
        launch._build_pack(ctx, {"email": "ann@demo.example", "name": "Ann Example", "contact_id": 1},
                           "2026-10-11T12:00:00Z", human_send=False)
    assert str(ei.value).startswith("unfilled template variables: ") and "{{unsubscribeUrl}}" in str(ei.value)

def _assert_unlaunched(tmp, cid):
    assert _sql(tmp, "SELECT status FROM campaigns WHERE id=?", (cid,))[0][0] == "tested"
    assert _sql(tmp, "SELECT lock_until IS NULL FROM campaigns WHERE id=?", (cid,))[0][0] == 1
    assert _sql(tmp, "SELECT COUNT(*) FROM campaign_batches WHERE campaign_id=?", (cid,))[0][0] == 0

def test_launch_path_substitutes_and_preflights(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch, settings
    packs, k = _capture(monkeypatch, tmp)
    calls = _count_push(monkeypatch)
    settings.set("email_footer_address", "1 Example Street")
    bad = _campaign(c, _eid(tmp), "<p>{{previewText}}</p>", "trial")
    _mark_tested(bad)
    with pytest.raises(launch.LaunchError) as ei:
        asyncio.run(launch.launch_campaign(bad))
    assert str(ei.value) == UNFILLED_PREVIEW_ERROR
    assert packs == []
    _assert_unlaunched(tmp, bad)
    assert calls == []
    _mark_tested(ids["c_badge"])
    with pytest.raises(_Stop):
        asyncio.run(launch.launch_campaign(ids["c_badge"]))
    assert packs
    for p in packs:
        m = _open(p, k)
        assert all("{{" not in m[f] for f in ("subject", "html", "text"))
        assert "Demo Co" in m["html"]

def _count_push(monkeypatch):
    from mailman.terminal import launch
    calls, inner = [], launch._push_packs
    async def _p(p, bearer):
        calls.append(len(p))
        return await inner(p, bearer)
    monkeypatch.setattr(launch, "_push_packs", _p)
    return calls

def test_launch_refuses_contact_data_tokens(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch, settings
    _capture(monkeypatch, tmp)
    calls = _count_push(monkeypatch)
    settings.set("email_footer_address", "1 Example Street")
    cid = ids["c_badge"]
    c.post(f"/admin/campaigns/{cid}/recipients", json={"by_tag": "trial"})
    _sql(tmp, "UPDATE contacts SET name=? WHERE email=?", ("Bob {{email}}", "bob@demo.example"))
    _sql(tmp, "UPDATE contacts SET name=? WHERE email=?", ("Cy {{x}}", "cy@demo.example"))
    _mark_tested(cid)
    with pytest.raises(launch.LaunchError) as ei:
        asyncio.run(launch.launch_campaign(cid))
    msg = str(ei.value)
    assert msg.startswith("2 contact(s) cannot be sent - "), msg
    assert "bob@demo.example ({{email}})" in msg and "cy@demo.example ({{x}})" in msg, msg
    assert "ann@demo.example" not in msg
    _assert_unlaunched(tmp, cid)
    assert calls == []
    for i in range(9):
        r = c.post("/admin/contacts", json={"email": f"extra{i}@demo.example", "name": f"Extra {i}",
                                            "tags": "cap", "consent_source": "demo"})
        assert r.status_code in (200, 201), r.text
    c.post(f"/admin/campaigns/{cid}/recipients", json={"by_tag": "cap"})
    _sql(tmp, "UPDATE contacts SET name='Q {{x}}' WHERE id IN "
              "(SELECT contact_id FROM campaign_recipients WHERE campaign_id=?)", (cid,))
    assert _sql(tmp, "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id=?", (cid,))[0][0] == 12
    with pytest.raises(launch.LaunchError) as ei:
        asyncio.run(launch.launch_campaign(cid))
    msg = str(ei.value)
    assert msg.startswith("12 contact(s) cannot be sent - "), msg
    assert msg.count("@demo.example ({{x}})") == 10, msg
    assert msg.endswith("; and 2 more - fix those contacts or remove them from the campaign"), msg
    _assert_unlaunched(tmp, cid)
    assert calls == []

def test_approve_next_refusal_keeps_batch_pending(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch, settings
    _capture(monkeypatch, tmp)
    pushed = []
    async def _ack(p, bearer):
        pushed.append(len(p))
        return {"accepted": len(p), "submitted": 0, "enqueued": 0, "deferred": 0}
    monkeypatch.setattr(launch, "_push_packs", _ack)
    settings.set("email_footer_address", "1 Example Street")
    settings.set("send_batch_size", "1")
    cid = ids["c_badge"]
    _mark_tested(cid)
    assert asyncio.run(launch.launch_campaign(cid))["batches"] == 2
    assert pushed == [1]
    b2 = _sql(tmp, "SELECT contact_id FROM campaign_recipients WHERE campaign_id=? AND batch_no=2", (cid,))[0][0]
    _sql(tmp, "UPDATE contacts SET name=? WHERE id=?", ("Z {{x}}", b2))
    _sql(tmp, "UPDATE campaign_batches SET status='drained', approve_unlock_at=NULL WHERE campaign_id=? AND batch_no=1", (cid,))
    b2_email = _sql(tmp, "SELECT email FROM contacts WHERE id=?", (b2,))[0][0]
    r = c.post(f"/admin/campaigns/{cid}/batches/2/approve")
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("1 contact(s) cannot be sent - "), r.text
    assert f"{b2_email} ({{{{x}}}})" in r.json()["detail"], r.text
    assert _sql(tmp, "SELECT status FROM campaign_batches WHERE campaign_id=? AND batch_no=2", (cid,))[0][0] == "pending"
    assert pushed == [1]

def test_relaunch_refuses_naming_contacts(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch, settings
    _capture(monkeypatch, tmp)
    pushed = []
    async def _ack(p, bearer):
        pushed.append(len(p))
        return {"accepted": len(p), "submitted": 0, "enqueued": 0, "deferred": 0}
    monkeypatch.setattr(launch, "_push_packs", _ack)
    settings.set("email_footer_address", "1 Example Street")
    cid = ids["c_badge"]
    _mark_tested(cid)
    asyncio.run(launch.launch_campaign(cid))
    assert pushed == [2]
    _sql(tmp, "UPDATE campaigns SET status='sent' WHERE id=?", (cid,))
    _sql(tmp, "UPDATE contacts SET name=? WHERE email=?", ("Bob {{x}}", "bob@demo.example"))
    with pytest.raises(launch.LaunchError) as ei:
        asyncio.run(launch.launch_campaign(cid, relaunch=True))
    msg = str(ei.value)
    assert msg.startswith("1 contact(s) cannot be sent - "), msg
    assert "bob@demo.example ({{x}})" in msg, msg
    assert pushed == [2]

def test_approve_next_ctx_has_company_name(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch
    _capture(monkeypatch, tmp)
    assert asyncio.run(launch._load_send_ctx(ids["c_badge"])).company_name == "Demo Co"

def test_test_send_substitutes_and_refuses(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import settings
    sent = _fake_httpx(monkeypatch)
    settings.set("resend_api_key", "re_x1")
    settings.set("email_footer_address", "1 Example Street")
    c.post(f"/admin/entities/{_eid(tmp)}/set-key", json={"resend_api_key": "re_x1"})
    r = c.post(f"/admin/campaigns/{ids['c_badge']}/test-send", json={"to_email": "t@demo.example"})
    assert r.status_code == 200, r.text
    assert len(sent) == 1
    assert all("{{" not in sent[0][f] for f in ("subject", "html", "text"))
    assert "Demo Co" in sent[0]["html"]
    bad = _campaign(c, _eid(tmp), "<p>{{previewText}}</p>", "trial")
    r = c.post(f"/admin/campaigns/{bad}/test-send", json={"to_email": "t@demo.example"})
    assert (r.status_code, r.json()["detail"]) == (400, UNFILLED_PREVIEW_ERROR)
    assert len(sent) == 1

def test_blank_entity_name_refuses_send(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch, settings
    eid = _eid(tmp)
    m2 = f"company {eid} has no name - set one on the Entities page before sending"
    packs, k = _capture(monkeypatch, tmp)
    sent = _fake_httpx(monkeypatch)
    settings.set("email_footer_address", "1 Example Street")
    settings.set("resend_api_key", "re_x1")
    _mark_tested(ids["c_badge"])
    for blank in ("", "   "):
        _sql(tmp, "UPDATE entities SET name=? WHERE id=?", (blank, eid))
        with pytest.raises(launch.LaunchError) as ei:
            launch.resolve_company_name(eid, {"subject": "", "html_body": "", "text_body": ""})
        assert str(ei.value) == m2, repr(blank)
        with pytest.raises(launch.LaunchError) as ei:
            asyncio.run(launch.launch_campaign(ids["c_badge"]))
        assert str(ei.value) == m2, repr(blank)
        assert packs == []
        r = c.post(f"/admin/campaigns/{ids['c_badge']}/test-send", json={"to_email": "t@demo.example"})
        assert (r.status_code, r.json()["detail"]) == (400, m2), repr(blank)
        assert sent == []

def test_global_campaign_company_name(ui):
    from mailman.terminal import launch
    with_token = {"subject": "Hi", "html_body": "<p>{{companyName}}</p>", "text_body": ""}
    without = {"subject": "Hi", "html_body": "<p>Hi {{name}}</p>", "text_body": ""}
    with pytest.raises(launch.LaunchError) as ei:
        launch.resolve_company_name(None, with_token)
    assert str(ei.value) == ("this template uses {{companyName}} but the campaign has no company"
                             " - choose a company for the campaign before sending")
    assert launch.resolve_company_name(None, without) is None
    assert launch._preflight(without, {"kind": "promotional"}, None) is None

def test_quicksend_refuses_tokens(ui, monkeypatch):
    c, ids, tmp = ui
    sent = _fake_httpx(monkeypatch)
    r = c.post("/admin/quicksend", json={"to_email": "t@demo.example", "subject": "Hi", "html_body": "<p>{{x}}</p>"})
    assert (r.status_code, r.json()["detail"]) == (
        400, "Quick send does not fill variables - remove every {{...}} from the subject and body")
    assert sent == []
    r = c.post("/admin/quicksend", json={"to_email": "t@demo.example", "subject": "Hi {{x}}", "html_body": "<p>Hi</p>"})
    assert (r.status_code, r.json()["detail"]) == (
        400, "Quick send does not fill variables - remove every {{...}} from the subject and body")
    assert sent == []

def test_preview_substitutes_company_name(ui):
    c, ids, tmp = ui
    r = c.get(f"/admin/templates/{ids['t_badge']}/preview/raw")
    assert r.status_code == 200
    assert "{{companyName}}" not in r.text and "MailMan" in r.text

def test_blank_entity_name_rejected(ui):
    c, ids, tmp = ui
    def count():
        return _sql(tmp, "SELECT COUNT(*) FROM entities")[0][0]
    eid, n0 = _eid(tmp), count()
    for blank in ("", "   "):
        r = c.post("/admin/entities", json={"name": blank, "domain": "x.example"})
        assert r.status_code == 422 and count() == n0, repr(blank)
        r = c.patch(f"/admin/entities/{eid}", json={"name": blank, "domain": "demo.example"})
        assert r.status_code == 422, repr(blank)
        assert _sql(tmp, "SELECT name FROM entities WHERE id=?", (eid,))[0][0] == "Demo Co"
        r = c.post("/admin/entities/bulk-import", files={"file": ("e.json", json.dumps(
            [{"name": blank, "domain": "x.example"}]).encode(), "application/json")})
        assert r.status_code == 200 and r.json()["imported"] == 0 and len(r.json()["errors"]) == 1, r.text
        assert count() == n0
    r = c.post("/admin/entities/create", data={"name": "   ", "domain": "x.example"}, follow_redirects=False)
    assert r.status_code == 303 and count() == n0
    r = c.post(f"/admin/entities/{eid}/update", data={"name": "   ", "domain": "demo.example"}, follow_redirects=False)
    assert r.status_code == 303
    assert _sql(tmp, "SELECT name FROM entities WHERE id=?", (eid,))[0][0] == "Demo Co"

def test_launch_error_maps_to_http_400(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import settings
    _capture(monkeypatch, tmp)
    settings.set("email_footer_address", "1 Example Street")
    bad = _campaign(c, _eid(tmp), "<p>{{previewText}}</p>", "trial")
    _mark_tested(bad)
    r = c.post(f"/admin/campaigns/{bad}/launch")
    assert r.status_code == 400 and "unfilled template variables" in r.json()["detail"], r.text
