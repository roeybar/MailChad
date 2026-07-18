"""Sync API endpoints - cloud side (spec §3, §6).

Short-poll GET /sync/pull, POST /sync/push, K_temp management, near-conflict
detection. All endpoints require bearer auth (§6) except /init/handshake
which uses a bootstrap token.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from mailchad.cloud import dynamo, keys_dynamo

log = logging.getLogger("cloud.sync")
router = APIRouter()

# Auth dependency

async def require_bearer(authorization: str | None = Header(default=None)) -> str:
    """Validate bearer token -> returns actor ('operator' | 'client').
    Touches last_seen_at on success.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer")
    bearer = authorization[len("Bearer "):].strip()
    bearer_hash = hashlib.sha256(bearer.encode()).hexdigest()
    row = dynamo.get_session_by_bearer(bearer_hash)
    if not row or row["revoked_at"]:
        raise HTTPException(401, "bearer not recognised or revoked")
    dynamo.touch_session(bearer_hash)
    return row["actor"]


# /sync/pull short-poll (GET)

@router.get("/sync/pull")
async def sync_pull(
    since: int = Query(0, ge=0),
    unsub_since: str = Query("", description="ISO ts cursor for the unsub cache"),
    actor: str = Depends(require_bearer),
):
    """Return events with event_id > since immediately. No long-poll.
    Terminal calls this on a short-poll interval (default 5s).

    Also returns UNSUB# cache rows added after unsub_since so terminals catch
    self-service unsubscribes (the public /u/ endpoint never writes to the
    sync stream - the bearer-authed pull is the only trusted path in).

    Returns: {"events": [...], "max_event_id": N,
              "unsubs": [{email_hash, scope, added_at}], "max_unsub_at": "..."}
    """
    rows = dynamo.query_events_since(since, limit=500)
    for r in rows:
        if r["encrypted_payload"] is not None:
            r["encrypted_payload"] = base64.b64encode(r["encrypted_payload"]).decode("ascii")

    unsubs = dynamo.list_unsubs(since_cursor=unsub_since, limit=1000)
    max_unsub_at = unsubs[-1]["cursor"] if unsubs else unsub_since

    return {
        "events": rows,
        "max_event_id": rows[-1]["event_id"] if rows else since,
        "unsubs": unsubs,
        "max_unsub_at": max_unsub_at,
    }


# /sync/push (POST)

class SyncEventIn(BaseModel):
    """Wire format per spec §3.1."""
    table:             str
    id:                str = Field(..., alias="row_id")  # accept "id" too via alias
    revision:          int
    actor:             str
    modified_at:       str
    encrypted_payload: str       # base64 - may be empty for tombstone post-GC
    key_id:            str
    deleted:           bool = False

    class Config:
        populate_by_name = True
        extra = "allow"


@router.post("/sync/push")
async def sync_push(
    events: list[dict],   # accept loose shape; we'll normalise
    actor: str = Depends(require_bearer),
):
    """Store new events. Each gets a fresh event_id. Detects near-conflicts (§3.5)
    against the most recent prior write to the same (table, row_id).

    Returns: {"accepted": N, "assigned_event_ids": [...], "near_conflicts": [...]}
    """
    accepted_ids = []
    near_conflicts = []
    for raw in events:
        # Normalise the loose dict from the wire
        table   = raw.get("table") or raw.get("table_name") or ""
        row_id  = raw.get("id") or raw.get("row_id") or ""
        rev     = int(raw.get("revision", 0))
        act     = raw.get("actor", actor)
        mod_at  = raw.get("modified_at", datetime.now(timezone.utc).isoformat())
        key_id  = raw.get("key_id", "K_op+K_cl")
        deleted = bool(raw.get("deleted"))
        payload_b64 = raw.get("encrypted_payload", "")
        payload = base64.b64decode(payload_b64) if payload_b64 else None

        if not table or not row_id or not act:
            raise HTTPException(400, "missing required field (table, row_id, or actor)")
        if act != actor:
            # bearer claims operator but payload says client (or vice versa) -> reject
            raise HTTPException(403, f"actor mismatch: bearer={actor!r} payload={act!r}")

        # Insert event
        event_id = dynamo.put_event(
            table_name=table, row_id=row_id, revision=rev, actor=act,
            modified_at=mod_at, key_id=key_id, encrypted_payload=payload,
            deleted=deleted,
        )
        accepted_ids.append(event_id)

        # Near-conflict detection: any prior write to (table, row_id) within 60s?
        prior = dynamo.query_latest_event_for_row(table, row_id, event_id)
        if prior:
            try:
                t_new = datetime.fromisoformat(mod_at.replace("Z", "+00:00"))
                t_old = datetime.fromisoformat(prior["modified_at"].replace("Z", "+00:00"))
                delta = abs((t_new - t_old).total_seconds())
            except Exception:
                delta = 9999.0
            if delta < 60 and prior["actor"] != act:
                dynamo.put_near_conflict(
                    table_name=table, row_id=row_id,
                    event_id_a=prior["event_id"], event_id_b=event_id,
                    actor_a=prior["actor"], actor_b=act,
                    modified_at_a=prior["modified_at"], modified_at_b=mod_at,
                    delta_seconds=delta,
                )
                near_conflicts.append({
                    "table": table, "row_id": row_id, "delta_s": delta,
                })
    return {
        "accepted":           len(accepted_ids),
        "assigned_event_ids": accepted_ids,
        "near_conflicts":     near_conflicts,
    }


# K_temp lifecycle endpoints (§2.3)

class KTempIn(BaseModel):
    k_temp_b64:  str       # base64-encoded 32 bytes
    ttl_seconds: int       # must be one of keys.VALID_TTLS


@router.post("/key/temp")
async def key_temp_set(body: KTempIn, actor: str = Depends(require_bearer)):
    """Terminal provisions a fresh K_temp. Replaces any active one (no overlap)."""
    try:
        k = base64.b64decode(body.k_temp_b64)
    except Exception as e:
        raise HTTPException(400, f"bad base64: {e}")
    try:
        meta = keys_dynamo.set_k_temp(k, body.ttl_seconds, set_by=actor)
    except keys_dynamo.TTLViolation as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"set": True, **meta}


@router.get("/key/temp/status")
async def key_temp_status(actor: str = Depends(require_bearer)):
    return keys_dynamo.k_temp_status()


@router.delete("/key/temp")
async def key_temp_revoke(actor: str = Depends(require_bearer)):
    """Manual wipe. Cloud also auto-wipes on TTL expiry inside get_active_k_temp."""
    keys_dynamo.wipe_k_temp()
    return {"revoked": True}


# Near-conflict review (§3.5)

@router.get("/sync/near-conflicts")
async def near_conflicts_list(
    unack_only: bool = True,
    limit: int = 200,
    actor: str = Depends(require_bearer),
):
    rows = dynamo.list_near_conflicts(unacked_only=unack_only, limit=limit)
    return {"near_conflicts": rows, "count": len(rows)}


@router.post("/sync/near-conflicts/{conflict_id}/ack")
async def near_conflicts_ack(conflict_id: int, actor: str = Depends(require_bearer)):
    ok = dynamo.ack_near_conflict(conflict_id, actor)
    if not ok:
        raise HTTPException(404, "conflict not found")
    return {"acked": True}
