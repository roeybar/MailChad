"""Public compliance endpoints - unsubscribe (RFC 8058) + GDPR erasure.

Ported from coherence/backend/auth/app/routes_compliance_public.py.

Differences from coherence version:
- writes to local SQLite cache (unsub_hash_cache, erasure_request_cache)
  instead of forwarding to a state-keeper over WG (vault is asleep)
- copy is brand-neutral (uses {_company()} env var)
- no JS, no external assets - server-rendered tiny HTML
"""

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mailchad.cloud import compliance_tokens, dynamo, settings
from mailchad.cloud.rate_limit import limiter

log = logging.getLogger("cloud.compliance")
router = APIRouter()


def _company() -> str:
    return (settings.get("entity_name") or settings.get("company_name") or "Your Company")


def _support_email() -> str:
    return (settings.get("support_email") or "support@example.com")


def _page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    html = (
        f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title>"
        f"<style>"
        f"*{{box-sizing:border-box;}} "
        f"body{{font-family:system-ui,-apple-system,sans-serif;max-width:480px;margin:5em auto;padding:0 1.5em;color:#1a1a1a;line-height:1.6;}}"
        f"h1{{font-size:1.2em;font-weight:700;margin:0 0 .75em 0;}}"
        f"p{{margin:.5em 0 1em 0;color:#444;font-size:.95em;}}"
        f".btn-row{{display:flex;flex-direction:column;gap:.6em;margin:1.25em 0;}}"
        f"button{{display:block;width:100%;border:0;padding:.75em 1.25em;font-size:.95em;font-weight:600;cursor:pointer;border-radius:6px;transition:opacity .15s;}}"
        f"button:hover{{opacity:.85;}} button:disabled{{opacity:.5;cursor:default;}}"
        f".btn-primary{{background:#1a1a1a;color:#fff;}}"
        f".btn-secondary{{background:#f0f0f0;color:#1a1a1a;}}"
        f".btn-danger{{background:#b91c1c;color:#fff;}}"
        f"#msg{{margin-top:1.25em;padding:.85em 1em;border-radius:6px;font-size:.9em;font-weight:500;display:none;}}"
        f"#msg.ok{{background:#dcfce7;color:#166534;display:block;}}"
        f"#msg.err{{background:#fee2e2;color:#991b1b;display:block;}}"
        f".dim{{color:#888;font-size:.82em;margin-top:1.5em;}}"
        f"a{{color:#555;}}"
        f"</style>"
        f"<h1>{title}</h1>{body_html}"
    )
    return HTMLResponse(html, status_code=status)


def _cache_unsub(email_hash: str, token: str) -> None:
    dynamo.put_unsub(email_hash, source_token=token)


def _cache_erasure(email_hash: str, token: str) -> None:
    dynamo.put_erasure(email_hash, source_token=token)


# Unsubscribe

@router.get("/u/{token}", response_class=HTMLResponse)
@limiter.limit("20/minute")
def unsub_landing(request: Request, token: str):
    try:
        email_hash = compliance_tokens.verify("unsub", token)
    except compliance_tokens.TokenExpired:
        return _page("Link expired",
            f"<p>This unsubscribe link has expired. Email <a href='mailto:{_support_email()}'>{_support_email()}</a> to opt out manually.</p>",
            status=410)
    except compliance_tokens.TokenInvalid:
        return _page("Invalid link",
            "<p>This unsubscribe link is not valid. Try the link from your most recent email.</p>",
            status=400)

    company = _company()
    support = _support_email()
    return _page(
        "Unsubscribe",
        f"<p>Choose what you want to stop receiving from <strong>{company}</strong>:</p>"
        f"<div class='btn-row'>"
        f"<button class='btn-primary' onclick=\"doUnsub('{token}','promotional',this)\">Unsubscribe from marketing emails</button>"
        f"<button class='btn-secondary' onclick=\"doUnsub('{token}','all',this)\">Unsubscribe from all emails</button>"
        f"</div>"
        f"<div id='msg'></div>"
        f"<p class='dim'>Marketing emails include newsletters and promotions. "
        f"Unsubscribing from all will also stop transactional messages such as receipts.<br><br>"
        f"Questions? <a href='mailto:{support}'>{support}</a></p>"
        f"<script>"
        f"function doUnsub(token,scope,btn){{"
        f"  btn.disabled=true;"
        f"  var other=btn.parentNode.querySelectorAll('button');"
        f"  other.forEach(function(b){{b.disabled=true;}});"
        f"  fetch('/u/'+token,{{method:'POST',headers:{{'Content-Type':'application/json'}},"
        f"    body:JSON.stringify({{scope:scope}})}}).then(function(r){{return r.json();}}).then(function(d){{"
        f"    var el=document.getElementById('msg');"
        f"    if(d.ok){{"
        f"      el.className='ok';"
        f"      el.textContent=scope==='all'?"
        f"        'Done. You have been unsubscribed from all {company} emails.':"
        f"        'Done. You have been unsubscribed from {company} marketing emails. Transactional messages will still arrive.';"
        f"    }}else{{"
        f"      el.className='err';"
        f"      el.textContent=d.error||'Something went wrong. Please try again.';"
        f"      other.forEach(function(b){{b.disabled=false;}});"
        f"    }}"
        f"  }}).catch(function(){{"
        f"    var el=document.getElementById('msg');"
        f"    el.className='err'; el.textContent='Network error. Please try again.';"
        f"    other.forEach(function(b){{b.disabled=false;}});"
        f"  }});"
        f"}}"
        f"</script>",
    )


@router.post("/u/{token}")
@limiter.limit("10/minute")
async def unsub_post(request: Request, token: str):
    """Handles both RFC 8058 one-click (form POST) and JS fetch (JSON body with scope)."""
    try:
        email_hash = compliance_tokens.verify("unsub", token)
    except compliance_tokens.TokenExpired:
        return HTMLResponse("expired", status_code=410)
    except compliance_tokens.TokenInvalid:
        return HTMLResponse("invalid", status_code=400)

    # Detect scope from JSON body (JS fetch) or default to 'all' (RFC 8058 one-click)
    scope = "all"
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            scope = body.get("scope", "all")
            if scope not in ("promotional", "all"):
                scope = "all"
        except Exception:
            pass

    try:
        dynamo.put_unsub(email_hash, token, scope=scope)
    except Exception as e:
        log.exception("unsub POST failed: %s", e)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Temporary error, please try again."}, status_code=503)

    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": True, "scope": scope})


# Erasure (GDPR Article 17)

@router.get("/e/{token}", response_class=HTMLResponse)
@limiter.limit("20/minute")
def erasure_landing(request: Request, token: str, confirm: int | None = None):
    try:
        email_hash = compliance_tokens.verify("erasure", token)
    except compliance_tokens.TokenExpired:
        return _page(
            "Link expired",
            f"<p class='err'>This erasure link has expired. Email <a href='mailto:{_support_email()}'>{_support_email()}</a> from the address you want erased and we will action it manually within 30 days (GDPR Article 12).</p>",
            status=410,
        )
    except compliance_tokens.TokenInvalid:
        return _page("Invalid link", "<p class='err'>This erasure link isn't valid.</p>", status=400)

    if confirm == 1:
        try:
            _cache_erasure(email_hash, token)
        except Exception as e:
            log.exception("erasure cache write failed: %s", e)
            return _page(
                "We couldn't record that",
                f"<p class='err'>Temporary error. Email <a href='mailto:{_support_email()}'>{_support_email()}</a>.</p>",
                status=503,
            )
        return _page(
            "Erasure requested",
            f"<p class='ok'>Your erasure request is recorded. Per GDPR Article 12, we will complete deletion within 30 days. You will not be contacted unless we need additional information to verify the request.</p>",
        )

    return _page(
        "Confirm data erasure",
        f"<p><b>Read this carefully.</b> Confirming will permanently delete all data {_company()} holds about you, including:</p>"
        f"<ul><li>your contact record</li><li>your email + send history</li><li>any consent records</li></ul>"
        f"<p>We will retain a one-way hash of your email address to honour your unsubscribe choice - this prevents accidentally re-emailing you if you appear in a future import. We cannot reverse this action.</p>"
        f"<form action='/e/{token}' method='post'>"
        f"<button type='submit' class='danger'>Yes, erase my data</button>"
        f"</form>"
        f"<p class='dim'>Or <a href='/e/{token}?confirm=1' class='btn danger'>confirm via GET</a>.</p>",
    )


@router.post("/e/{token}")
@limiter.limit("10/minute")
def erasure_post(request: Request, token: str):
    try:
        email_hash = compliance_tokens.verify("erasure", token)
    except compliance_tokens.TokenExpired:
        return HTMLResponse("expired", status_code=410)
    except compliance_tokens.TokenInvalid:
        return HTMLResponse("invalid", status_code=400)
    try:
        _cache_erasure(email_hash, token)
    except Exception as e:
        log.exception("erasure POST cache write failed: %s", e)
        return HTMLResponse(f"error: {e}", status_code=503)
    return HTMLResponse("ok", status_code=200)
