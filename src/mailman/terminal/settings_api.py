"""Terminal settings REST API - session-protected (section 12).

Terminal's admin UI calls these to read/write its own settings.
All routes require a valid operator session (cookie auth).

Shape mirrors cloud/app/settings_api.py; helps the UI use the same
shape for both cloud + terminal settings.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from mailman.terminal import settings, db
from mailman.terminal.models import Setting
from mailman.terminal.auth import require_session

log = logging.getLogger("terminal.settings_api")
router = APIRouter(prefix="/admin/settings/api")


@router.get("")
def list_settings(session=Depends(require_session)):
    return {"settings": settings.get_all(redact_secrets=True),
            "secret_keys": sorted(settings.SECRET_KEYS)}


@router.get("/{key}")
def get_setting(key: str, session=Depends(require_session)):
    if settings.is_secret(key):
        with db.session() as s:
            row = s.execute(select(1).select_from(Setting).where(Setting.key == key, Setting.is_secret == 1)).first()
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
