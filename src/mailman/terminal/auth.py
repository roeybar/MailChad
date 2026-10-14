"""Minimal admin auth for vault.

Single-operator model. Email + bcrypt-hashed
password in env, JWT cookie session.

ADMIN_EMAIL: plaintext email/handle
ADMIN_PASSWORD_HASH: bcrypt hash (helper: scripts/v3 admin-bcrypt; see Known limitations in the README)
JWT_SECRET: base64-48

Ported in spirit from email-platform/src/lib/auth/.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import base64

import bcrypt
from fastapi import APIRouter, Cookie, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select, update

from mailman.terminal import db, settings
from mailman.terminal.models import Operator

log = logging.getLogger("terminal.auth")
router = APIRouter()

SESSION_COOKIE = "v3_session"


def _admin_email() -> str:
    return settings.get("admin_email", "") or ""


def _admin_password_hash() -> str:
    return settings.get("admin_password_hash", "") or ""


def _jwt_secret() -> str:
    return settings.get("jwt_secret", "") or ""


def _session_ttl_s() -> int:
    return settings.get_int("session_ttl_s", 86400)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload: dict) -> str:
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET not set in settings - set via admin UI")
    payload_b = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = _b64u(payload_b)
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u(sig)}"


def _verify(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    secret = _jwt_secret()
    if not secret:
        return None
    expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u_decode(sig_b64), expected):
        return None
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def require_session(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """FastAPI dep: requires a valid session cookie."""
    payload = _verify(session) if session else None
    if not payload:
        raise HTTPException(
            status.HTTP_307_TEMPORARY_REDIRECT,
            "auth required",
            headers={"Location": f"/admin/auth/login"},
        )
    return payload


def require_admin(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """FastAPI dep: requires a valid session with admin role."""
    payload = require_session(request, session)
    if payload.get("role", "admin") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return payload


# Routes

def login_page(error: str | None = None) -> HTMLResponse:
    err_html = (
        f'<div class="callout crit" style="margin-bottom:16px">'
        f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:17px;height:17px;flex-shrink:0;margin-top:1px"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
        f'<div class="ct-body">{error}</div></div>'
    ) if error else ""
    mail_svg = '<img src="data:image/png;base64,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" width="26" height="26" style="border-radius:5px;display:block;filter:invert(1)">'
    lock_svg = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>'
    return HTMLResponse(f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/png" href="/favicon.ico">
<title>Sign in \u00B7 MailMan</title>
<script>(function(){{try{{var t=localStorage.getItem('mc_theme')||'light';document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}}})();</script>
<style>
:root{{--font-sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--bg:oklch(0.984 0.002 285);--surface:oklch(1 0 0);--surface-2:oklch(0.975 0.003 285);--border:oklch(0.918 0.004 285);--border-2:oklch(0.862 0.006 285);--text:oklch(0.262 0.012 285);--text-muted:oklch(0.505 0.012 285);--text-dim:oklch(0.62 0.01 285);--accent:oklch(0.55 0.205 286);--accent-hover:oklch(0.48 0.205 286);--accent-press:oklch(0.43 0.2 286);--accent-soft:oklch(0.955 0.032 286);--accent-soft-2:oklch(0.925 0.052 286);--accent-border:oklch(0.86 0.07 286);--on-accent:oklch(0.99 0.005 286);--crit-fg:oklch(0.5 0.18 27);--crit-bg:oklch(0.955 0.045 27);--crit-bd:oklch(0.87 0.08 27);--shadow-sm:0 1px 2px oklch(0.2 0.02 285/0.06),0 1px 1px oklch(0.2 0.02 285/0.04);--shadow-lg:0 16px 48px oklch(0.2 0.02 285/0.18),0 4px 12px oklch(0.2 0.02 285/0.1);--ring:0 0 0 3px var(--accent-soft-2);--r-sm:6px;--r-lg:12px;--r-xl:16px;color-scheme:light}}
[data-theme="dark"]{{color-scheme:dark;--bg:oklch(0.178 0.008 285);--surface:oklch(0.212 0.009 285);--surface-2:oklch(0.246 0.011 285);--border:oklch(0.305 0.012 285);--border-2:oklch(0.38 0.016 285);--text:oklch(0.945 0.004 285);--text-muted:oklch(0.71 0.012 285);--text-dim:oklch(0.575 0.012 285);--accent:oklch(0.7 0.16 286);--accent-hover:oklch(0.77 0.15 286);--accent-press:oklch(0.82 0.13 286);--accent-soft:oklch(0.3 0.05 286);--accent-soft-2:oklch(0.36 0.07 286);--on-accent:oklch(0.16 0.02 286);--crit-fg:oklch(0.8 0.13 27);--crit-bg:oklch(0.34 0.08 27);--crit-bd:oklch(0.46 0.1 27);--shadow-lg:0 20px 56px oklch(0 0 0/0.6),0 6px 16px oklch(0 0 0/0.4)}}
*,*::before,*::after{{box-sizing:border-box}}*{{margin:0}}body{{font-family:var(--font-sans);background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}
.login-wrap{{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(120% 120% at 50% 0%,var(--accent-soft) 0%,var(--bg) 45%)}}
.login-card{{width:100%;max-width:384px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-xl);box-shadow:var(--shadow-lg);padding:32px}}
.mark{{width:34px;height:34px;border-radius:9px;background:linear-gradient(150deg,var(--accent),var(--accent-press));display:grid;place-items:center;color:var(--on-accent);box-shadow:var(--shadow-sm)}}
.field{{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}}label{{font-size:12.5px;font-weight:550;color:var(--text)}}
input{{width:100%;background:var(--surface);color:var(--text);border:1px solid var(--border-2);border-radius:var(--r-sm);padding:8px 11px;font-size:13.5px;font-family:inherit;transition:border-color .12s,box-shadow .12s}}input::placeholder{{color:var(--text-dim)}}
input:focus{{outline:none;border-color:var(--accent);box-shadow:var(--ring)}}
.btn{{display:flex;align-items:center;justify-content:center;width:100%;padding:9px 14px;border-radius:var(--r-sm);border:none;font:inherit;font-size:13px;font-weight:550;cursor:pointer;background:var(--accent);color:var(--on-accent);box-shadow:var(--shadow-sm);margin-top:6px;transition:background .12s}}
.btn:hover{{background:var(--accent-hover)}}.btn:active{{background:var(--accent-press)}}
.icon-btn{{appearance:none;width:32px;height:32px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface);display:grid;place-items:center;cursor:pointer;color:var(--text-muted)}}
.callout.crit{{display:flex;gap:11px;padding:12px 15px;border-radius:var(--r-sm);background:var(--crit-bg);border:1px solid var(--crit-bd);color:var(--crit-fg);font-size:13px}}
.ct-body{{min-width:0}}
.dim{{color:var(--text-dim);font-size:12px}}
</style>
</head>
<body>
<button class="icon-btn" id="themeToggle" style="position:fixed;top:18px;right:18px" title="Toggle theme" aria-label="Toggle theme"></button>
<div class="login-wrap">
  <div class="login-card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:22px">
      <span class="mark">{mail_svg}</span>
      <div>
        <div style="font-weight:650;font-size:17px;letter-spacing:-0.03em">MailMan</div>
        <div class="dim">MailMan v4 terminal</div>
      </div>
    </div>
    <h1 style="font-size:19px;letter-spacing:-0.025em;margin-bottom:4px">Sign in to your vault</h1>
    <p style="color:var(--text-muted);font-size:13.5px;margin-bottom:20px">The terminal encrypts rendered send jobs; the cloud dispatcher decrypts them with a temporary key to call Resend. Open tracking is off. Sign in to manage your campaigns.</p>
    {err_html}
    <form method="post" action="/admin/auth/login">
      <div class="field"><label for="email">Email</label><input type="text" id="email" name="email" autofocus autocomplete="username"></div>
      <div class="field"><label for="password">Password</label><input type="password" id="password" name="password" placeholder="\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" autocomplete="current-password"></div>
      <button class="btn" type="submit">Sign in</button>
    </form>
    <div style="display:flex;align-items:center;gap:7px;margin-top:18px;justify-content:center;color:var(--text-dim);font-size:12px">
      {lock_svg} JWT sessions \u00B7 bcrypt passwords \u00B7 secret settings encrypted at rest
    </div>
  </div>
</div>
<script>(function(){{
  var sun='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
  var moon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
  function apply(t){{var r=document.documentElement;r.classList.add('theme-switching');r.setAttribute('data-theme',t);requestAnimationFrame(function(){{requestAnimationFrame(function(){{r.classList.remove('theme-switching');}});}});try{{localStorage.setItem('mc_theme',t);}}catch(e){{}}var btn=document.getElementById('themeToggle');if(btn)btn.innerHTML=t==='dark'?sun:moon;}}
  var btn=document.getElementById('themeToggle');var cur=document.documentElement.getAttribute('data-theme')||'light';if(btn){{btn.innerHTML=cur==='dark'?sun:moon;btn.addEventListener('click',function(){{apply(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');}});}}
}})();</script>
</body>
</html>""")


@router.get("/admin/auth/login", response_class=HTMLResponse)
def login_page_get(error: str | None = None):
    return login_page(error)


def _check_operator(email: str, password: str) -> dict | None:
    """Returns operator row dict if credentials match, else None."""
    try:
        with db.session() as s:
            row = s.execute(
                select(Operator.id, Operator.email, Operator.password_hash, Operator.role, Operator.active)
                .where(Operator.email == email.strip().lower())
            ).mappings().first()
        if not row:
            return None
        if not row["active"]:
            return None
        if bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            return dict(row)
    except Exception as e:
        log.warning("operator check error: %s", e)
    return None


def _update_last_login(operator_id: int) -> None:
    try:
        with db.session() as s:
            s.execute(
                update(Operator).where(Operator.id == operator_id)
                .values(last_login_at=func.datetime("now")).execution_options(synchronize_session=False)
            )
            s.commit()
    except Exception:
        pass


@router.post("/admin/auth/login")
def login(response: Response, email: str = Form(...), password: str = Form(...)):
    jwt_secret = _jwt_secret()
    ttl_s      = _session_ttl_s()
    if not jwt_secret:
        raise HTTPException(503, "jwt_secret not set - configure via admin UI")

    # Try operators table first
    op = _check_operator(email, password)
    if op:
        _update_last_login(op["id"])
        token = _sign({"sub": op["email"], "role": op["role"],
                       "iat": int(time.time()), "exp": int(time.time()) + ttl_s})
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=False, max_age=ttl_s, path="/")
        return resp

    # Legacy fallback: single admin from settings
    admin_email = _admin_email()
    admin_hash  = _admin_password_hash()
    if not (admin_email and admin_hash):
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)
    if email.strip().lower() != admin_email.strip().lower():
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)
    try:
        ok = bcrypt.checkpw(password.encode(), admin_hash.encode())
    except Exception as e:
        log.warning("bcrypt error: %s", e)
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)
    if not ok:
        return RedirectResponse("/admin/auth/login?error=Invalid+credentials", status_code=303)

    token = _sign({"sub": email, "role": "admin",
                   "iat": int(time.time()), "exp": int(time.time()) + ttl_s})
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=False, max_age=ttl_s, path="/")
    return resp


@router.post("/admin/auth/logout")
def logout(response: Response):
    response = RedirectResponse("/admin/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
