"""Pack API - cloud side (§4).

Terminals POST encrypted packs to /packs. On receipt the pack is stored in
DynamoDB and a {"pack_id": "<id>"} message is enqueued to SQS. The
ep-dispatcher Lambda (or local poller) drains that queue.

K_temp lifecycle and plaintext window logic live in dispatcher_lambda.py.
"""
from __future__ import annotations

import base64
import json
import logging
import os

import boto3
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mailchad.cloud import dynamo
from mailchad.cloud.sync import require_bearer

log = logging.getLogger("cloud.packs")
router = APIRouter()

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_ENDPOINT  = os.environ.get("SQS_ENDPOINT")


def _sqs():
    kwargs = dict(region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    if SQS_ENDPOINT:
        kwargs["endpoint_url"] = SQS_ENDPOINT
    return boto3.client("sqs", **kwargs)


def _enqueue(pack_id: str) -> None:
    if not SQS_QUEUE_URL:
        log.warning("SQS_QUEUE_URL not set - pack %s not enqueued", pack_id)
        return
    try:
        _sqs().send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps({"pack_id": pack_id}),
        )
    except Exception as e:
        log.warning("SQS enqueue failed for %s: %s", pack_id, e)


# /packs endpoints (terminal-facing)

class PackIn(BaseModel):
    pack_id:        str
    campaign_id:    int
    recipient_hash: str       # sha256(recipient.lower()) - for drift without leak
    content_hash:   str       # sha256(subject + html) - for drift detection
    send_at:        str       # ISO8601 UTC
    key_id:         str       # which K_temp encrypts this
    encrypted_blob: str       # base64 of the encryption envelope


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.post("/packs")
async def packs_push(packs: list[PackIn], actor: str = Depends(require_bearer)):
    """Terminal pushes encrypted send-jobs. Idempotent on pack_id.

    A pack already due (send_at <= now) is enqueued to SQS immediately. A pack
    with a future send_at is left `pending` in DynamoDB - the EventBridge-driven
    dispatcher sweep (list_pending_packs via the status/send_at GSI, §4.2) feeds it
    to SQS when it comes due. This is what lets the cloud hold thousands of delayed
    sends and fire each when scheduled, with the terminal uninvolved after push.
    """
    now = _now_iso()
    inserted = 0
    enqueued = 0
    deferred = 0
    for p in packs:
        try:
            dynamo.put_pack(
                pack_id=p.pack_id, campaign_id=p.campaign_id,
                recipient_hash=p.recipient_hash, content_hash=p.content_hash,
                send_at=p.send_at, key_id=p.key_id,
                encrypted_payload=base64.b64decode(p.encrypted_blob),
                pushed_by=actor,
            )
            if (p.send_at or "") <= now:
                _enqueue(p.pack_id)
                enqueued += 1
            else:
                deferred += 1            # sweep will enqueue when due
            inserted += 1
        except dynamo.PackAlreadyExists:
            continue
    return {"accepted": inserted, "submitted": len(packs),
            "enqueued": enqueued, "deferred": deferred}


@router.get("/packs/status")
async def packs_status(since_id: str = "", limit: int = 500,
                        actor: str = Depends(require_bearer)):
    """Terminals pull dispatch results so they can update local campaign_recipients."""
    rows = dynamo.list_resolved_packs(since_enqueued_at=since_id, limit=limit)
    return {"packs": rows, "count": len(rows),
            "max_id": rows[-1]["enqueued_at"] if rows else since_id}


@router.delete("/packs/{pack_id}")
async def packs_cancel(pack_id: str, actor: str = Depends(require_bearer)):
    """Cancel a pending pack. No-op if claimed/sent/failed."""
    pack = dynamo.get_pack(pack_id)
    if not pack:
        raise HTTPException(404, "no such pack")
    if pack["status"] != "pending":
        return {"cancelled": False, "reason": f"already {pack['status']}"}
    dynamo.update_pack(pack_id, status="cancelled", encrypted_payload=None)
    return {"cancelled": True}


@router.post("/packs/cancel-campaign")
async def packs_cancel_campaign(body: dict, actor: str = Depends(require_bearer)):
    """Cancel ALL pending packs for a campaign - including salted seed packs that have
    no terminal dispatched_job row. The correct campaign-abort path."""
    campaign_id = body.get("campaign_id")
    if campaign_id is None:
        raise HTTPException(400, "campaign_id required")
    n = dynamo.cancel_pending_for_campaign(int(campaign_id))
    return {"cancelled": n, "campaign_id": int(campaign_id)}


