"""Settings save/auth/cloud endpoints - no longer has its own pages.

Settings are now distributed to the feature pages that own them:
  Brand + global sender + cloud  -> /admin/entities  (Config section)
  Unsub/erasure secrets + TTLs   -> /admin/suppression (Config section)
  Session TTL + JWT rotate       -> /admin/operators   (Config section)
  K_temp TTL                     -> /admin             (Config section)

Old /admin/settings/* routes redirect to the new locations.
POST /admin/settings/{key}/save and POST /admin/settings/auth/* kept here.
"""
from __future__ import annotations

import base64
import logging
import os

import bcrypt
import httpx
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from mailchad.terminal import db, settings, encryption
from mailchad.terminal.auth import require_session

log = logging.getLogger("terminal.admin_settings")
router = APIRouter()

CLOUD_URL = os.environ.get("CLOUD_URL", "http://cloud:8443")


# Redirect shims - old bookmarks keep working

@router.get("/admin/settings")
@router.get("/admin/settings/brand")
@router.get("/admin/settings/webhooks")
def _redirect_to_entities():
    return RedirectResponse("/admin/entities", status_code=303)


@router.get("/admin/settings/secrets")
@router.get("/admin/settings/tuning")
def _redirect_to_suppression():
    return RedirectResponse("/admin/suppression", status_code=303)


@router.get("/admin/settings/cloud")
def _redirect_cloud_to_entities():
    return RedirectResponse("/admin/entities", status_code=303)


@router.get("/admin/settings/auth")
def _redirect_to_operators():
    return RedirectResponse("/admin/operators", status_code=303)


# Reveal secret (linked from suppression page)

@router.get("/admin/settings/reveal/{key}", response_class=HTMLResponse)
def reveal_secret(key: str, session=Depends(require_session)):
    """Show decrypted value of a secret (audit-logged). Back link -> suppression."""
    import html as _h
    if not settings.is_secret(key):
        raise HTTPException(400, f"{key!r} is not a secret")
    v = settings.get(key)
    log.warning("admin reveal: %s revealed %s", session.get("sub", "?"), key)
    html = f"""<!doctype html><meta charset='utf-8'><title>Reveal {key}</title>
<style>body{{font-family:system-ui;max-width:700px;margin:3em auto;padding:0 1em;color:#222}}
.warn{{background:#fef;border-left:3px solid #c6c;padding:.7em 1em;margin:1em 0;font-size:.9em}}
table{{border-collapse:collapse;width:100%;margin:1em 0}}
th,td{{text-align:left;padding:.5em .8em;border-bottom:1px solid #eee}}
th{{background:#f7f7f7;width:30%}}</style>
<h1 style='font-size:1.3em'>Reveal: {_h.escape(key)}</h1>
<div class='warn'>Reveal logged. Don't copy this into chat or email - paste straight where you need it and close this tab.</div>
<table>
<tr><th>Key</th><td><code>{_h.escape(key)}</code></td></tr>
<tr><th>Decrypted value</th><td><code style='word-break:break-all'>{_h.escape(v or '(empty)')}</code></td></tr>
</table>
<p><a href='/admin/suppression'>← back to Suppression</a></p>
"""
    return HTMLResponse(html)


# Per-key save (called by all inline config forms)

CROSS_SIDE_SYNC: set[str] = {"unsub_secret", "erasure_secret"}

_BRAND   = {"entity_name", "support_email", "public_host", "email_footer_address", "email_from"}
_AUTH    = {"admin_email"}
_SECRETS = {"unsub_secret", "erasure_secret", "resend_api_key"}
_TUNING_SUP  = {"unsub_token_ttl_s", "erasure_token_ttl_s"}
_TUNING_OPS  = {"session_ttl_s"}
_TUNING_OVW  = {"default_k_temp_ttl_s"}
_SENDING     = {"send_window_start_hour", "send_window_hours", "send_window_tz",
                "send_sender_count", "send_rush_tail_minutes", "send_batch_size",
                "send_jitter_min_s", "send_jitter_max_s", "send_rush_jitter_s"}


def _redirect_after_save(key: str, cloud_status: str | None) -> str:
    suffix = f"?ok={key}" + (f"&cloud={cloud_status}" if cloud_status else "")
    if key in _BRAND or key in _SECRETS and key == "resend_api_key":
        return f"/admin/entities{suffix}"
    if key in _SECRETS or key in _TUNING_SUP:
        return f"/admin/suppression{suffix}"
    if key in _AUTH or key in _TUNING_OPS:
        return f"/admin/operators{suffix}"
    if key in _TUNING_OVW:
        return f"/admin{suffix}"
    if key in _SENDING:
        return f"/admin/settings/sending{suffix}"
    return f"/admin/entities{suffix}"


def _push_to_cloud(key: str, value: str) -> tuple[bool, str]:
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    bearer = bearer_path.read_text().strip() if bearer_path.exists() else ""
    if not bearer:
        return False, "no cloud bearer"
    try:
        r = httpx.post(f"{CLOUD_URL}/settings",
                       json={"key": key, "value": value},
                       headers={"Authorization": f"Bearer {bearer}"},
                       timeout=10)
        if r.status_code >= 400:
            return False, f"cloud rejected: HTTP {r.status_code}"
        return True, "synced to cloud"
    except Exception as e:
        return False, f"network: {e}"


@router.post("/admin/settings/{key}/save")
def settings_save(key: str, session=Depends(require_session), value: str = Form(...)):
    actor = f"operator:{session.get('sub', '?')}"
    settings.set(key, value, updated_by=actor)

    cloud_status = None
    if key in CROSS_SIDE_SYNC:
        ok, msg = _push_to_cloud(key, value)
        cloud_status = "synced" if ok else f"fail:{msg}"
        if not ok:
            log.warning("settings_save: %s saved locally; cloud sync failed: %s", key, msg)

    return RedirectResponse(_redirect_after_save(key, cloud_status), status_code=303)


# Auth mutations (change password, rotate JWT)

@router.post("/admin/settings/auth/password")
def change_password(session=Depends(require_session),
                    new_password: str = Form(...), confirm: str = Form(...)):
    if new_password != confirm:
        return RedirectResponse("/admin/operators?error=mismatch", status_code=303)
    if len(new_password) < 12:
        return RedirectResponse("/admin/operators?error=too_short", status_code=303)
    h = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    settings.set("admin_password_hash", h, updated_by=f"operator:{session.get('sub', '?')}")
    return RedirectResponse("/admin/operators?ok=password", status_code=303)


@router.post("/admin/settings/auth/rotate-jwt")
def rotate_jwt(session=Depends(require_session)):
    import secrets as _secrets
    new = base64.b64encode(_secrets.token_bytes(48)).decode()
    settings.set("jwt_secret", new, updated_by=f"operator:{session.get('sub', '?')}")
    response = RedirectResponse("/admin/auth/login?ok=jwt_rotated", status_code=303)
    response.delete_cookie("v3_session", path="/")
    return response


# Cloud cross-call save

@router.post("/admin/settings/cloud/save")
def cloud_save(session=Depends(require_session), key: str = Form(...), value: str = Form(...)):
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    bearer = bearer_path.read_text().strip() if bearer_path.exists() else ""
    if not bearer:
        return RedirectResponse("/admin/entities?error=no_bearer", status_code=303)
    try:
        r = httpx.post(f"{CLOUD_URL}/settings",
                       json={"key": key, "value": value},
                       headers={"Authorization": f"Bearer {bearer}"},
                       timeout=10)
        r.raise_for_status()
    except Exception as e:
        return RedirectResponse(f"/admin/entities?error={e}", status_code=303)
    return RedirectResponse("/admin/entities?ok=cloud", status_code=303)
