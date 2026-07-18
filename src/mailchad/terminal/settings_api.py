"""Terminal settings REST API - session-protected (§Q1 Phase B).

Terminal's admin UI calls these to read/write its own settings.
All routes require a valid operator session (cookie auth).

Shape mirrors cloud/app/settings_api.py; helps the UI use the same
shape for both cloud + terminal settings.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mailchad.terminal import settings, db
from mailchad.terminal.auth import require_session

log = logging.getLogger("terminal.settings_api")
router = APIRouter(prefix="/admin/settings/api")


@router.get("")
def list_settings(session=Depends(require_session)):
    return {"settings": settings.get_all(redact_secrets=True),
            "secret_keys": sorted(settings.SECRET_KEYS)}


@router.get("/{key}")
def get_setting(key: str, session=Depends(require_session)):
    if settings.is_secret(key):
        with db.conn() as c:
            row = c.execute("SELECT 1 FROM settings WHERE key = ? AND is_secret = 1", (key,)).fetchone()
        present = bool(row)
        return {"key": key, "is_secret": True, "value": "<encrypted>" if present else None}
    return {"key": key, "is_secret": False, "value": settings.get(key)}


@router.get("/{key}/reveal")
def reveal_setting(key: str, session=Depends(require_session)):
    v = settings.get(key)
    log.warning("settings.reveal: %s revealed key=%s",
                session.get("sub", "?"), key)
    return {"key": key, "is_secret": settings.is_secret(key), "value": v}


class SettingIn(BaseModel):
    key: str
    value: str


@router.post("")
def upsert_setting(body: SettingIn, session=Depends(require_session)):
    actor = f"operator:{session.get('sub','?')}"
    settings.set(body.key, body.value, updated_by=actor)
    return {"upserted": body.key, "is_secret": settings.is_secret(body.key)}


@router.delete("/{key}")
def delete_setting(key: str, session=Depends(require_session)):
    actor = f"operator:{session.get('sub','?')}"
    settings.delete(key)
    log.info("settings.delete: %s deleted key=%s", actor, key)
    return {"deleted": key}
