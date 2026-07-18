"""ep-dispatcher Lambda handler - SQS consumer (spec §4.2 + Phase 3).

Each SQS record body: {"pack_id": "<uuid>"}

Batch size 10 (configured on trigger). Each record processed independently;
exceptions per-record mark that message for retry up to max_receive_count,
then DLQ. Idempotent: if pack is not 'pending' on arrival, skip silently.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("dispatcher")

RESEND_API_URL = "https://api.resend.com/emails"
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
SQS_ENDPOINT  = os.environ.get("SQS_ENDPOINT")
SWEEP_LIMIT   = int(os.environ.get("SWEEP_LIMIT", "200"))


def _sqs():
    import boto3
    kwargs = {}
    if SQS_ENDPOINT:
        kwargs["endpoint_url"] = SQS_ENDPOINT
    return boto3.client("sqs", **kwargs)


def _enqueue(pack_id: str) -> bool:
    if not SQS_QUEUE_URL:
        log.warning("sweep: SQS_QUEUE_URL unset - cannot enqueue %s", pack_id)
        return False
    try:
        _sqs().send_message(QueueUrl=SQS_QUEUE_URL,
                            MessageBody=json.dumps({"pack_id": pack_id}))
        return True
    except Exception as e:
        log.warning("sweep: enqueue failed for %s: %s", pack_id, e)
        return False


def _sweep() -> dict:
    """EventBridge-driven: find packs now due (send_at <= now, status=pending) via
    the status/send_at GSI and feed them to SQS. This is the timed feed §4.2 always
    specified - it lets the cloud hold thousands of delayed sends and fire each when
    due, with the terminal uninvolved. Idempotent: claim/status guards in _process
    make a double-enqueue a no-op."""
    from mailchad.cloud import dynamo
    due = dynamo.list_pending_packs(limit=SWEEP_LIMIT)
    n = sum(1 for p in due if _enqueue(p["pack_id"]))
    log.info("sweep: %d due pack(s) enqueued", n)
    return {"swept": len(due), "enqueued": n}

# In-memory bounce tracker: {campaign_id: {"sent": N, "bounced": N, "delay_s": X}}
# Shared within a process (local dispatcher). Lambda resets per invocation - acceptable.
_campaign_stats: dict[int, dict] = {}


def handler(event: dict, context) -> dict:
    """Lambda entry point. Also called directly by dispatcher_local.py.

    Two trigger shapes:
      - SQS event (has Records) -> drain + send (the existing path).
      - EventBridge scheduled / {"mode":"sweep"} -> run the due-pack sweep.
    """
    if not event.get("Records") or event.get("mode") == "sweep" \
            or event.get("source") == "aws.events":
        return _sweep()

    records = event.get("Records", [])
    results = {"processed": 0, "skipped": 0, "failed": 0}

    i = 0
    while i < len(records):
        # Process one record to get sender_count and delay for this group
        record = records[i]
        try:
            pack_id = json.loads(record["body"])["pack_id"]
        except Exception as e:
            log.error("bad SQS record: %s - %s", record.get("body", "?"), e)
            results["failed"] += 1
            i += 1
            continue

        outcome, delay, sender_count = _process(pack_id)
        results[outcome] = results.get(outcome, 0) + 1
        i += 1

        # Process remaining senders in this group (no sleep between them)
        for _ in range(1, sender_count):
            if i >= len(records):
                break
            try:
                next_pack_id = json.loads(records[i]["body"])["pack_id"]
            except Exception as e:
                log.error("bad SQS record: %s", e)
                results["failed"] += 1
                i += 1
                continue
            out, _, _ = _process(next_pack_id)
            results[out] = results.get(out, 0) + 1
            i += 1

        # Sleep once after the full sender group (if more records remain)
        if delay > 0 and i < len(records):
            log.info("human-send: sleeping %.1fs after group of %d", delay, sender_count)
            time.sleep(delay)

    log.info("batch done: %s", results)
    return results


def _process(pack_id: str) -> tuple[str, float]:
    """Process one pack. Returns (outcome, sleep_seconds_before_next)."""
    from mailchad.cloud import dynamo, keys_dynamo
    from mailchad.cloud.encryption_cloud import decrypt_with_temp

    pack = dynamo.get_pack(pack_id)
    if not pack:
        log.warning("pack %s not found - skipping", pack_id)
        return "skipped", 0.0, 1
    if pack["status"] != "pending":
        log.info("pack %s already %s - idempotent skip", pack_id, pack["status"])
        return "skipped", 0.0, 1

    claimed = dynamo.claim_pack(pack_id)
    if not claimed:
        log.info("pack %s: race - another worker claimed it", pack_id)
        return "skipped", 0.0, 1

    pack = dynamo.get_pack(pack_id)

    k_temp = keys_dynamo.get_active_k_temp()
    if not k_temp:
        dynamo.update_pack(pack_id, status="stuck_no_key",
                           failure_reason="no active K_temp at dispatch time")
        log.warning("pack %s: stuck_no_key", pack_id)
        return "processed", 0.0, 1

    plain = None
    rate_per_min = None
    bounce_pause_pct = 0.10
    campaign_id = None
    try:
        plain_bytes = decrypt_with_temp(pack["encrypted_payload"], k_temp)
        plain = json.loads(plain_bytes)
        rate_per_min = plain.get("rate_limit_per_min")
        bounce_pause_pct = plain.get("bounce_pause_pct") or 0.10
        campaign_id = plain.get("campaign_id")
        human_send = plain.get("human_send", False)
        human_min = plain.get("human_send_min_s", 60)
        human_max = plain.get("human_send_max_s", 210)
        human_count = int(plain.get("human_send_count") or 1)
    except Exception as e:
        dynamo.update_pack(pack_id, status="failed",
                           failure_reason=f"decrypt failed: {e}",
                           encrypted_payload=None)
        log.warning("pack %s: decrypt failed: %s", pack_id, e)
        return "processed", 0.0, 1

    status, msg_id, failure = _send_sync(plain)
    plain = None  # discard plaintext immediately

    _finalize(pack_id, status, msg_id, failure)
    _update_campaign_stats(campaign_id, bounced=(status == "failed"))

    delay = _compute_delay(rate_per_min, bounce_pause_pct, campaign_id,
                           human_send, human_min, human_max)
    return "processed", delay, human_count


def _update_campaign_stats(campaign_id: int | None, bounced: bool) -> None:
    if campaign_id is None:
        return
    s = _campaign_stats.setdefault(campaign_id, {"sent": 0, "bounced": 0})
    s["sent"] += 1
    if bounced:
        s["bounced"] += 1


def _compute_delay(rate_per_min: int | None, bounce_pause_pct: float, campaign_id: int | None,
                   human_send: bool = False, human_min: int = 60, human_max: int = 210) -> float:
    if human_send:
        base = random.uniform(human_min, human_max)
        log.debug("human-send delay: %.1fs (range %s–%ss)", base, human_min, human_max)
    elif rate_per_min:
        base = 60.0 / rate_per_min
    else:
        base = 0.0

    if campaign_id is not None:
        s = _campaign_stats.get(campaign_id, {})
        sent = s.get("sent", 0)
        bounced = s.get("bounced", 0)
        if sent > 10 and bounced / sent >= bounce_pause_pct:
            log.warning("campaign %s: bounce rate %.1f%% ≥ threshold %.1f%% - emergency rate halving",
                        campaign_id, bounced / sent * 100, bounce_pause_pct * 100)
            base = max(base, 1.0) * 2
    return base


def _send_sync(plain: dict) -> tuple[str, str | None, str | None]:
    """Synchronous Resend POST. Returns (status, message_id, failure_reason)."""
    api_key = plain.get("resend_api_key")
    if not api_key:
        return "failed", None, "pack missing resend_api_key"

    body = {
        "from":           plain["from"],
        "to":             [plain["recipient"]],
        "subject":        plain["subject"],
        "html":           plain["html"],
        "headers":        plain.get("headers", {}),
        # Opens are bot/MPP noise (image proxies + security scanners fire the pixel),
        # so we don't track them at all. Engagement = body-button clicks only.
        "open_tracking":  False,
        "click_tracking": True,
    }
    if plain.get("text"):
        body["text"] = plain["text"]

    try:
        r = httpx.post(RESEND_API_URL, json=body,
                       headers={"Authorization": f"Bearer {api_key}"},
                       timeout=30)
    except Exception as e:
        # Network error - let SQS retry (raise so visibility timeout resets)
        raise RuntimeError(f"network: {e}") from e

    if r.status_code >= 500:
        raise RuntimeError(f"resend {r.status_code} - will retry")

    try:
        j = r.json()
    except Exception:
        j = {}

    if r.status_code >= 400:
        return "failed", None, j.get("message", f"resend {r.status_code}")
    return "sent", j.get("id"), None


def _finalize(pack_id: str, status: str, msg_id: str | None, failure: str | None) -> None:
    from mailchad.cloud import dynamo
    now_iso = datetime.now(timezone.utc).isoformat()
    dynamo.update_pack(
        pack_id,
        status=status,
        sent_at=now_iso if status == "sent" else None,
        resend_message_id=msg_id,
        failure_reason=failure,
        encrypted_payload=None,
    )
    if status == "sent":
        dynamo.put_event(
            table_name="pack", row_id=pack_id, revision=1, actor="cloud",
            modified_at=now_iso, key_id="K_op+K_cl",
            encrypted_payload=None, deleted=False,
        )
    log.info("pack %s -> %s (msg_id=%s)", pack_id, status, msg_id)
