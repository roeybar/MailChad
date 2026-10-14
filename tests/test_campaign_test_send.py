"""Campaign test mail uses the same rendered pack and refusals as a launch."""
from __future__ import annotations
import asyncio, base64, hashlib, json, random, re, sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
SUB = "card-tester@example.com"
BADGE = "ENCRYPTED END-TO-END"
PLACEHOLDER = "This is a test send from MailMan campaign"
UNFILLED_PREVIEW_ERROR = "unfilled template variables: {{previewText}} - use only the variables listed on the Templates page"

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

def _sql(tmp, q, args=()):
    con = sqlite3.connect(str(tmp / "state.sqlite"))
    try:
        rows = con.execute(q, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()

def _open(p, k):
    import base64
    from mailman.terminal import encryption
    return json.loads(encryption.decrypt_with_temp(base64.b64decode(p["encrypted_blob"]), k))

def _eid(tmp):
    return _sql(tmp, "SELECT id FROM entities WHERE name='Demo Co'")[0][0]

def _campaign(c, eid, html_body, tag):
    t = c.post("/admin/templates", json={"name": f"T {tag}", "subject": "Hi", "from_name": "Demo Co",
                                         "html_body": html_body, "text_body": "x"}).json()["id"]
    cid = c.post("/admin/campaigns", json={"name": f"C {tag}", "template_id": t, "entity_id": eid}).json()["id"]
    c.post(f"/admin/campaigns/{cid}/recipients", json={"by_tag": tag})
    return cid

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


def _fake_resend(monkeypatch, status=200, exc=None):
    import httpx
    sent = []
    original = httpx.post
    def _post(url, **kw):
        if url != "https://api.resend.com/emails":
            return original(url, **kw)
        sent.append((kw["headers"], kw["json"]))
        if exc is not None:
            raise exc
        return httpx.Response(status, json={"id": f"m-{len(sent)}"})
    monkeypatch.setattr(httpx, "post", _post)
    return sent


def _launch_capture(monkeypatch, tmp):
    from mailman.terminal import encryption, launch
    k = encryption.mint_k_temp()
    packs = []
    async def _k(bearer):
        return k
    async def _push(p, bearer):
        packs.extend(p)
        return {"accepted": len(p), "submitted": 0, "enqueued": 0, "deferred": 0}
    monkeypatch.setattr(launch, "_ensure_k_temp", _k)
    monkeypatch.setattr(launch, "_push_packs", _push)
    (tmp / "keys" / "cloud_bearer.txt").write_text("tok")
    return packs, k


def _ready(c, tmp):
    from mailman.terminal import settings
    settings.set("email_footer_address", "1 Example Street")
    r = c.post(f"/admin/entities/{_eid(tmp)}/set-key", json={"resend_api_key": "re_fa"})
    assert r.status_code == 200, r.text


def _norm(s):
    return re.sub(r"/(u|e)/[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", r"/\1/TOKEN", s)


def _send(c, cid, email="ann@demo.example"):
    return c.post(f"/admin/campaigns/{cid}/test-send", json={"to_email": email})


def _audit(tmp, cid):
    return [json.loads(r[0]) for r in _sql(tmp, "SELECT details_json FROM audit_event "
            "WHERE action='campaign.test_sent' AND target=? ORDER BY id", (f"campaign:{cid}",))]


def _assert_no_placeholder(sent):
    assert all(PLACEHOLDER not in json.dumps(body) for _, body in sent)


def test_test_mail_equals_launch_mail_for_contact(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch, routes_admin
    _ready(c, tmp)
    cid = _campaign(c, _eid(tmp), "<p>Hello {{name}}</p><p>{{companyName}}</p>", "newsletter")
    sent = _fake_resend(monkeypatch)
    packs, k = _launch_capture(monkeypatch, tmp)
    r = _send(c, cid)
    assert r.status_code == 200, r.text
    assert len(sent) == 1
    body = sent[0][1]
    routes_admin.mark_tested(cid)
    asyncio.run(launch.launch_campaign(cid))
    plain = {_open(p, k)["recipient"]: _open(p, k) for p in packs}
    ann, bob = plain["ann@demo.example"], plain["bob@demo.example"]
    assert _norm(body["html"]) == _norm(ann["html"])
    assert _norm(body["text"]) == _norm(ann["text"])
    assert set(body["headers"]) == set(ann["headers"])
    assert {h: _norm(v) for h, v in body["headers"].items()} == {
        h: _norm(v) for h, v in ann["headers"].items()}
    assert body["from"] == ann["from"] == "Demo Co <hello@demo.example>"
    assert body["subject"] == "[Test] " + ann["subject"]
    assert body["to"] == ["ann@demo.example"]
    for html in (body["html"], ann["html"]):
        assert "/u/TOKEN" in _norm(html) and "/e/TOKEN" in _norm(html)
        assert PLACEHOLDER not in html
    assert _norm(body["html"]) != _norm(bob["html"])
    _assert_no_placeholder(sent)


def test_test_mail_equals_launch_mail_for_seed(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch, routes_admin
    _ready(c, tmp)
    r = c.post("/admin/seeds", json={"email": "seed@demo.example"})
    assert r.status_code == 200, r.text
    cid = _campaign(c, _eid(tmp), "<p>Hello {{name}}</p><p>{{email}}</p>", "newsletter")
    _sql(tmp, "UPDATE templates SET text_body='Hello {{name}} {{email}}' "
              "WHERE id=(SELECT template_id FROM campaigns WHERE id=?)", (cid,))
    sent = _fake_resend(monkeypatch)
    packs, k = _launch_capture(monkeypatch, tmp)
    assert _send(c, cid, "seed@demo.example").status_code == 200
    routes_admin.mark_tested(cid)
    asyncio.run(launch.launch_campaign(cid))
    seeds = [_open(p, k) for p in packs if _open(p, k)["recipient"] == "seed@demo.example"]
    assert len(seeds) == 1
    body, seed = sent[0][1], seeds[0]
    for field in ("html", "text"):
        assert _norm(body[field]) == _norm(seed[field])
        assert "Ann Example" not in body[field] and "Bob Sample" not in body[field]
    assert "<p>Hello </p>" in body["html"]
    assert _send(c, cid, "Seed@Demo.example").status_code == 200
    upper = sent[1][1]
    assert upper["to"] == ["Seed@Demo.example"]
    assert upper["subject"] == body["subject"] and upper["from"] == body["from"]
    assert set(upper["headers"]) == set(body["headers"])
    assert {h: _norm(v) for h, v in upper["headers"].items()} == {
        h: _norm(v) for h, v in body["headers"].items()}
    for field in ("html", "text"):
        assert _norm(upper[field]) != _norm(body[field])
        assert _norm(upper[field]).replace("Seed@Demo.example", "seed@demo.example") == _norm(body[field])
    _assert_no_placeholder(sent)


def test_test_mail_tokens_bound_to_address(ui, monkeypatch):
    c, ids, tmp = ui
    _ready(c, tmp)
    sent = _fake_resend(monkeypatch)
    assert _send(c, ids["c_clean"]).status_code == 200
    html = sent[0][1]["html"]
    for kind in ("u", "e"):
        tokens = re.findall(r"/" + kind + r"/([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+", html)
        assert tokens
        for token in tokens:
            payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            assert payload["h"] == hashlib.sha256(b"ann@demo.example").hexdigest()
            assert payload["h"] != hashlib.sha256(b"bob@demo.example").hexdigest()
    assert "test-preview" not in html
    _assert_no_placeholder(sent)


@pytest.mark.parametrize("case", ["missing_company", "blank_company", "company_token_without_company",
                         "preview_token", "subject_token", "contact_token", "contact_cap",
                         "missing_footer", "company_key", "no_key", "seed_control"])
def test_refusal_parity(ui, monkeypatch, case):
    c, ids, tmp = ui
    from mailman.terminal import launch, routes_admin, settings
    _ready(c, tmp)
    eid = _eid(tmp)
    html = "<p>Hello {{name}}</p>"
    if case == "company_token_without_company":
        html = "<p>{{companyName}}</p>"
    elif case == "preview_token":
        html = "<p>{{previewText}}</p>"
    cid = _campaign(c, eid, html, "newsletter")
    control_eid = c.post("/admin/entities", json={"name": "Control Co", "domain": "demo.example",
                         "from_name": "Control Co", "from_email": "hello@demo.example"}).json()["id"]
    r = c.post(f"/admin/entities/{control_eid}/set-key", json={"resend_api_key": "re_control"})
    assert r.status_code == 200, r.text
    control = _campaign(c, control_eid, "<p>Hello {{name}}</p>", "trial")
    if case == "missing_company":
        _sql(tmp, "UPDATE campaigns SET entity_id=9999 WHERE id=?", (cid,))
    elif case == "blank_company":
        _sql(tmp, "UPDATE entities SET name='' WHERE id=?", (eid,))
    elif case == "company_token_without_company":
        _sql(tmp, "UPDATE campaigns SET entity_id=NULL WHERE id=?", (cid,))
        settings.set("resend_api_key", "re_global")
    elif case == "subject_token":
        _sql(tmp, "UPDATE templates SET subject='Hi {{nope}}' "
                  "WHERE id=(SELECT template_id FROM campaigns WHERE id=?)", (cid,))
    elif case == "contact_token":
        _sql(tmp, "UPDATE contacts SET name='Bob {{x}}' WHERE email='bob@demo.example'")
    elif case == "contact_cap":
        for i in range(12):
            r = c.post("/admin/contacts", json={"email": f"bad{i}@demo.example", "name": "Bad {{x}}",
                       "tags": "bad", "consent_source": "demo"})
            assert r.status_code == 200, r.text
        r = c.post(f"/admin/campaigns/{cid}/recipients", json={"by_tag": "bad"})
        assert r.status_code == 200, r.text
    elif case == "missing_footer":
        settings.set("email_footer_address", "")
        _sql(tmp, "UPDATE campaigns SET kind='transactional' WHERE id=?", (control,))
    elif case == "company_key":
        _sql(tmp, "UPDATE entities SET resend_key_enc=NULL WHERE id=?", (eid,))
        settings.set("resend_api_key", "re_global")
    elif case == "no_key":
        _sql(tmp, "UPDATE campaigns SET entity_id=NULL WHERE id=?", (cid,))
        _sql(tmp, "UPDATE entities SET resend_key_enc=NULL WHERE id=?", (eid,))
        settings.set("resend_api_key", "")
    elif case == "seed_control":
        r = c.post("/admin/seeds", json={"email": "seed@demo.example"})
        assert r.status_code == 200, r.text
    sent = _fake_resend(monkeypatch)
    packs, k = _launch_capture(monkeypatch, tmp)
    routes_admin.mark_tested(cid)
    if case == "seed_control":
        asyncio.run(launch.launch_campaign(cid))
        assert packs
        r = _send(c, cid)
        assert r.status_code == 200, r.text
        assert len(sent) == 1 and len(_audit(tmp, cid)) == 1
    else:
        with pytest.raises(launch.LaunchError) as ei:
            asyncio.run(launch.launch_campaign(cid))
        expected = str(ei.value)
        r = _send(c, cid)
        assert (r.status_code, r.json()["detail"]) == (400, expected)
        assert sent == [] and _audit(tmp, cid) == [] and packs == []
        if case == "missing_company":
            assert expected == "company 9999 not found"
        elif case == "preview_token":
            assert expected == UNFILLED_PREVIEW_ERROR
        elif case == "contact_cap":
            assert expected.startswith("12 contact(s) cannot be sent") and "; and 2 more" in expected
    before = len(sent)
    r = _send(c, control, "cy@demo.example")
    assert r.status_code == 200, r.text
    assert len(sent) == before + 1
    _assert_no_placeholder(sent)


def test_placeholder_never_sent(ui, monkeypatch):
    c, ids, tmp = ui
    _ready(c, tmp)
    doc = c.get("/admin/campaigns")
    assert doc.status_code == 200
    assert "'/admin/campaigns/'+_testCampId+'/test-send'" in doc.text
    assert "body:JSON.stringify({to_email:email})" in doc.text
    assert "fetch('/admin/quicksend'" not in doc.text and PLACEHOLDER not in doc.text
    for path in (ROOT / "src").rglob("*"):
        if path.is_file():
            assert PLACEHOLDER.encode() not in path.read_bytes(), path
    sent = _fake_resend(monkeypatch)
    for email in ("ann@demo.example", "bob@demo.example", "seed@demo.example"):
        assert _send(c, ids["c_clean"], email).status_code == 200
    assert len(sent) == 3
    _assert_no_placeholder(sent)


def test_test_sent_audit(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import routes_admin
    _ready(c, tmp)
    sent = _fake_resend(monkeypatch)
    cid = ids["c_clean"]
    assert _send(c, cid).status_code == 200
    current = _sql(tmp, "SELECT template_hash FROM templates WHERE id=?", (ids["t_clean"],))[0][0]
    rows = _audit(tmp, cid)
    assert rows == [{"template_hash": current, "message_id": "m-1"}]
    assert "@" not in json.dumps(rows)
    bad = _campaign(c, _eid(tmp), "<p>{{previewText}}</p>", "trial")
    r = _send(c, bad)
    assert (r.status_code, r.json()["detail"]) == (400, UNFILLED_PREVIEW_ERROR)
    assert len(sent) == 1 and _audit(tmp, bad) == []
    for status, exc, expected in ((422, None, 400), (200, RuntimeError("offline"), 500)):
        with monkeypatch.context() as m:
            rejected = _fake_resend(m, status=status, exc=exc)
            r = _send(c, ids["c_badge"])
            assert r.status_code == expected, r.text
            assert len(rejected) == 1 and _audit(tmp, ids["c_badge"]) == []
    assert _audit(tmp, bad) == []
    assert routes_admin.mark_tested(bad)["tested"] is True
    assert _sql(tmp, "SELECT status FROM campaigns WHERE id=?", (bad,)) == [("tested",)]
    assert _audit(tmp, bad) == []


def test_resend_failures(ui, monkeypatch):
    c, ids, tmp = ui
    _ready(c, tmp)
    for status, exc, expected in ((422, None, (400, "Resend rejected: ")),
                                  (200, RuntimeError("offline"), (500, "send failed: offline"))):
        with monkeypatch.context() as m:
            sent = _fake_resend(m, status=status, exc=exc)
            r = _send(c, ids["c_clean"])
            assert r.status_code == expected[0]
            if exc is None:
                assert r.json()["detail"].startswith(expected[1])
                assert "m-1" in r.json()["detail"]
            else:
                assert r.json()["detail"] == expected[1]
            assert len(sent) == 1 and _audit(tmp, ids["c_clean"]) == []


def test_input_and_lookup(ui, monkeypatch):
    c, ids, tmp = ui
    _ready(c, tmp)
    sent = _fake_resend(monkeypatch)
    r = _send(c, ids["c_clean"], "nobody")
    assert (r.status_code, r.json()["detail"]) == (400, "invalid email")
    r = _send(c, 999999)
    assert (r.status_code, r.json()["detail"]) == (404, "no such campaign")
    assert sent == []
    assert _send(c, ids["c_clean"], "  ann@demo.example  ").status_code == 200
    assert sent[0][1]["to"] == ["ann@demo.example"]
    assert _send(c, ids["c_clean"], "ANN@demo.example").status_code == 200
    assert sent[1][1]["to"] == ["ANN@demo.example"]
    assert "Hello Ann Example" in sent[1][1]["html"]
    assert _norm(sent[1][1]["html"]) == _norm(sent[0][1]["html"])
    _assert_no_placeholder(sent)


def test_test_send_changes_nothing(ui, monkeypatch):
    c, ids, tmp = ui
    from mailman.terminal import launch
    _ready(c, tmp)
    cid = ids["c_badge"]
    sent = _fake_resend(monkeypatch)
    packs, k = _launch_capture(monkeypatch, tmp)
    calls, original = [], launch._push_packs
    async def _push(p, bearer):
        calls.append(p)
        return await original(p, bearer)
    monkeypatch.setattr(launch, "_push_packs", _push)
    def snapshot():
        return (_sql(tmp, "SELECT * FROM campaigns WHERE id=?", (cid,)),
                _sql(tmp, "SELECT * FROM campaign_recipients WHERE campaign_id=? ORDER BY contact_id", (cid,)),
                [_sql(tmp, f"SELECT COUNT(*) FROM {table}") for table in
                 ("dispatched_job", "campaign_batches", "suppression_hashes")])
    before = snapshot()
    for _ in range(3):
        r = _send(c, cid)
        assert r.status_code == 200, r.text
    assert snapshot() == before
    assert len(sent) == 3 and packs == [] and calls == []


def test_stress_mixed_and_boundary(ui, monkeypatch):
    c, ids, tmp = ui
    _ready(c, tmp)
    from mailman.terminal import settings
    settings.set("resend_api_key", "re_global")
    clean = _campaign(c, _eid(tmp), "<p>Hello {{name}}</p>", "newsletter")
    bad = _campaign(c, _eid(tmp), "<p>{{previewText}}</p>", "trial")
    no_company = _campaign(c, None, "<p>{{companyName}}</p>", "vip")
    missing_company = ("this template uses {{companyName}} but the campaign has no company"
                       " - choose a company for the campaign before sending")
    sent = _fake_resend(monkeypatch)
    rng = random.Random(7)
    expected = {clean: (200, None), bad: (400, UNFILLED_PREVIEW_ERROR), no_company: (400, missing_company)}
    addresses = ["ann@demo.example", "bob@demo.example", "seed@demo.example", "new@x.example",
                 "  Ann@Demo.Example "]
    successes = 0
    for _ in range(60):
        cid, email = rng.choice([clean, bad, no_company]), rng.choice(addresses)
        r = _send(c, cid, email)
        status, detail = expected[cid]
        assert r.status_code == status, (cid, email, r.text)
        if detail is None:
            successes += 1
            assert r.json()["sent"] is True
        else:
            assert r.json()["detail"] == detail
    assert len(sent) == successes == sum(len(_audit(tmp, cid)) for cid in expected)
    for _, body in sent:
        assert all("{{" not in body.get(field, "") for field in ("subject", "html", "text"))
    _assert_no_placeholder(sent)
    for email in ("x" * 10000 + "@d.example", "\u00e9@demo.example", "@"):
        r = _send(c, clean, email)
        assert r.status_code != 500, (email, r.text)
    before_calls, before_rows = len(sent), len(_audit(tmp, clean))
    with ThreadPoolExecutor(4) as pool:
        responses = list(pool.map(lambda _: _send(c, clean), range(12)))
    assert all(r.status_code == 200 for r in responses), [(r.status_code, r.text) for r in responses]
    assert len(sent) == before_calls + 12
    assert len(_audit(tmp, clean)) == before_rows + 12
    _assert_no_placeholder(sent)
    assert all("{{" not in body.get(field, "") for _, body in sent for field in ("subject", "html", "text"))


def test_campaigns_page_test_modal(ui):
    c, ids, tmp = ui
    r = c.get("/admin/campaigns")
    assert r.status_code == 200
    doc = r.text
    assert ("Sends this campaign's own template, filled in for this address the way a launch fills it, "
            "with working unsubscribe and delete-my-data links. Only this address gets it.") in doc
    assert "var allOk=chain.template_ok&&chain.cloud_ok;" in doc
    assert "None yet: the first launch makes one. A test send does not need it." in doc
    assert "function _tsEsc(" in doc and "_tsMsg(sd)" in doc
    for cid in (ids["c_badge"], ids["c_clean"]):
        assert f"campAction({cid},'mark-tested')" not in doc
    assert "Mark as tested" in doc
    assert "campAction('+_testCampId+" in doc
    scripts = inline_scripts(doc)
    assert scripts
    assert [err for script in scripts for err in js_lex_errors(script)] == []
