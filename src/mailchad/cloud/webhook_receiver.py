"""Resend webhook receiver - v3.1 (cloud-side, §4.3 + §13.6).

Differences from v3.0:
- Encrypts each webhook event to BOTH K_op_pub AND K_cl_pub (so either
  terminal can decrypt locally, but cloud cannot decrypt after writing).
- Inserts as a sync event into event_log (table_name='webhook_event'),
  picked up by terminals on next /sync poll.
- Webhook events survive past K_temp TTL because they use K_op+K_cl mode.
- Also keeps a row in webhook_event_raw for audit + idempotency on svix_id.
- Auto-suppression (hard-bounce + complaint + unsubscribe) writes a
  *separate* sync event (table_name='suppression'), encrypted same way.
  Terminals materialise into their local suppression_hashes table.

Auth: HMAC-SHA256 svix signature with RESEND_WEBHOOK_SECRET. Fail-closed
when secret unset. Replay window: WEBHOOK_MAX_SKEW_S (default 600s).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from mailchad.cloud import dynamo, encryption_cloud, settings
from mailchad.cloud.rate_limit import limiter

log = logging.getLogger("cloud.webhook")
router = APIRouter()

HARD_BOUNCE_TYPES = {"email.bounced"}
COMPLAINT_TYPES   = {"email.complained"}
UNSUBSCRIBE_TYPES = {"email.unsubscribed"}


def _all_secrets() -> list[bytes]:
    """Return all configured webhook secrets (comma-separated, one per Resend account)."""
    raw = settings.get("resend_webhook_secret") or ""
    secrets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("whsec_"):
            part = part[len("whsec_"):]
        try:
            secrets.append(base64.b64decode(part))
        except Exception:
            secrets.append(part.encode("utf-8"))
    return secrets


def _verify_signature(*, svix_id: str, svix_timestamp: str,
                      body: bytes, signature_header: str) -> bool:
    secrets = _all_secrets()
    if not secrets:
        return False
    signed = f"{svix_id}.{svix_timestamp}.{body.decode('utf-8', errors='replace')}".encode("utf-8")
    for secret in secrets:
        expected = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode("ascii")
        for entry in signature_header.split():
            if not entry.startswith("v1,"):
                continue
            if hmac.compare_digest(entry[len("v1,"):], expected):
                return True
    return False


def _write_event_log(table_name: str, row_id: str, encrypted_payload: bytes) -> int:
    """Append a sync event. Returns event_id. Uses revision=0 + actor='system'.

    Note: actor='system' would violate the CHECK constraint (operator|client only).
    For webhook events we use actor='client' as the conventional "system" stand-in
    so that the operator's terminal also pulls them via existing actor-mismatch
    skip logic (it doesn't filter by actor). This is a known shorthand -
    proper fix is a separate `actor` value or schema CHECK relaxation.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dynamo.put_event(
        table_name=table_name, row_id=row_id, revision=0,
        actor="client", modified_at=now_iso, key_id="K_op+K_cl",
        encrypted_payload=encrypted_payload, deleted=False,
    )


@router.post("/webhooks/resend")
@limiter.limit("200/minute")
async def resend_webhook(request: Request):
    svix_id        = request.headers.get("svix-id", "")
    svix_timestamp = request.headers.get("svix-timestamp", "")
    svix_signature = request.headers.get("svix-signature", "")

    if not (svix_id and svix_timestamp and svix_signature):
        raise HTTPException(400, detail="missing svix-* headers")
    try:
        ts = int(svix_timestamp)
    except ValueError:
        raise HTTPException(400, detail="svix-timestamp not int")
    now = int(time.time())
    max_skew = settings.get_int("webhook_max_skew_s", 600)
    if abs(now - ts) > max_skew:
        raise HTTPException(400, detail=f"timestamp skew {now - ts}s exceeds {max_skew}s")

    body = await request.body()
    if not _verify_signature(svix_id=svix_id, svix_timestamp=svix_timestamp,
                             body=body, signature_header=svix_signature):
        # Fail closed
        raise HTTPException(401, detail="signature verification failed")

    try:
        event = json.loads(body)
    except Exception as e:
        raise HTTPException(400, detail=f"body not JSON: {e}") from e

    event_type = event.get("type", "")
    data       = event.get("data") or {}
    message_id = data.get("email_id") or data.get("id")
    recipients = data.get("to") or []
    recipient  = recipients[0] if recipients else None

    # Idempotency on svix_id via webhook_event_raw
    existing = dynamo.get_webhook_by_svix(svix_id)
    if existing:
        return {"ok": True, "duplicate": True}

    # Load pubkeys (fail loudly if not registered)
    try:
        pubkeys = encryption_cloud.load_pubkeys()
    except RuntimeError as e:
        log.error("webhook arrived but pubkeys not registered: %s", e)
        # Still 200 to Resend so they don't retry forever; raw row will be
        # recovered later (operator visibility).
        dynamo.put_webhook_raw(svix_id=svix_id, event_type=event_type, message_id=message_id)
        return {"ok": True, "warning": "pubkeys not registered - event saved to raw audit only"}

    # 1. Encrypt the event for both terminals + write as sync event
    event_payload = {
        "svix_id":     svix_id,
        "event_type":  event_type,
        "message_id":  message_id,
        "recipient":   recipient,
        "raw":         body.decode("utf-8", errors="replace"),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    encrypted = encryption_cloud.encrypt_for_both(
        json.dumps(event_payload).encode(),
        pubkeys=pubkeys,
    )
    row_id = svix_id   # idempotent across replays
    event_id = _write_event_log("webhook_event", row_id, encrypted)

    # 2. Auto-suppression: hard-bounce + complaint + unsubscribe
    side = {"event_type": event_type, "suppression": None}
    if recipient and (event_type in HARD_BOUNCE_TYPES
                      or event_type in COMPLAINT_TYPES
                      or event_type in UNSUBSCRIBE_TYPES):
        h = hashlib.sha256(recipient.lower().encode()).hexdigest()
        reason = ("bounce_hard" if event_type in HARD_BOUNCE_TYPES
                  else "complaint" if event_type in COMPLAINT_TYPES
                  else "unsubscribe")
        suppression_payload = {
            "email_hash":  h,
            "reason":      reason,
            "source":      f"webhook:{event_type}",
            "added_at":    datetime.now(timezone.utc).isoformat(),
        }
        suppression_encrypted = encryption_cloud.encrypt_for_both(
            json.dumps(suppression_payload).encode(),
            pubkeys=pubkeys,
        )
        _write_event_log("suppression", h, suppression_encrypted)
        side["suppression"] = reason

    # 3. Raw audit row (links to the sync event_id for traceability)
    dynamo.put_webhook_raw(svix_id=svix_id, event_type=event_type, message_id=message_id, forwarded_event_id=event_id)

    return {"ok": True, "forwarded_event_id": event_id, **side}
