"""Cloud settings REST API - bearer-protected (§Q1 Phase B).

Terminal's admin UI calls these to read/write cloud-side settings remotely.
All routes require a valid terminal bearer (registered at handshake).

  GET    /settings                    list (secrets redacted)
  GET    /settings/{key}              read one (redacted if secret)
  GET    /settings/{key}/reveal       read decrypted (audit-logged)
  POST   /settings                    {key, value} - upsert
  DELETE /settings/{key}              wipe
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mailchad.cloud import dynamo, settings
from mailchad.cloud.sync import require_bearer

log = logging.getLogger("cloud.settings_api")
router = APIRouter(prefix="/settings")


@router.get("")
async def list_settings(actor: str = Depends(require_bearer)):
    """All settings; secrets redacted. Always-safe to display."""
    return {"settings": settings.get_all(redact_secrets=True),
            "secret_keys": sorted(settings.SECRET_KEYS)}


@router.get("/{key}")
async def get_setting(key: str, actor: str = Depends(require_bearer)):
    """Read one setting. Secrets returned as '<encrypted>'."""
    if settings.is_secret(key):
        row = dynamo.get_setting(key)
        present = row is not None and row["is_secret"]
        return {"key": key, "is_secret": True, "value": "<encrypted>" if present else None}
    v = settings.get(key)
    return {"key": key, "is_secret": False, "value": v}


@router.get("/{key}/reveal")
async def reveal_setting(key: str, actor: str = Depends(require_bearer)):
    """Decrypted read. Audit-logged. Use sparingly - meant for the 'show key' button."""
    v = settings.get(key)
    log.warning("settings.reveal: %s revealed key=%s", actor, key)
    return {"key": key, "is_secret": settings.is_secret(key), "value": v}


class SettingIn(BaseModel):
    key: str
    value: str


@router.post("")
async def upsert_setting(body: SettingIn, actor: str = Depends(require_bearer)):
    settings.set(body.key, body.value, updated_by=actor)
    return {"upserted": body.key, "is_secret": settings.is_secret(body.key)}


@router.delete("/{key}")
async def delete_setting(key: str, actor: str = Depends(require_bearer)):
    settings.delete(key)
    log.info("settings.delete: %s deleted key=%s", actor, key)
    return {"deleted": key}
