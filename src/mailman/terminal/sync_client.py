"""Terminal-side sync client - short-poll loop + push (spec section 3.2).

Runs as a background asyncio task. Two responsibilities:

  1. PULL - short-poll cloud /sync/pull?since=N -> receive events past cursor ->
     decrypt with KeyBundle -> apply to local DB -> advance cursor.

  2. PUSH - drain local outbox table -> POST /sync/push -> on success,
     mark outbox rows as sent.

Idempotent + resumable: cursor stored in local DB. Reconnect = replay
from cursor. An event is skipped only when its exact (table_name, row_id,
revision) is already in the inbox; older unseen revisions are stored too.

Conflict resolution: never client-side. Cloud detects sub-minute writes
to same (table, row_id), writes to near_conflict_log. Terminal pulls
those flags via separate endpoint and surfaces in UI.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Callable

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from mailman.terminal import db, encryption
from mailman.terminal.models import (Campaign, CampaignBatch, CampaignRecipient, CampaignStage, Contact,
                                      DispatchedJob, DriftReport, InboxRow, OutboxRow, SuppressionHash, SyncState)

log = logging.getLogger("terminal.sync")

CLOUD_URL = os.environ.get("CLOUD_URL", "http://cloud:8443")
TERMINAL_ACTOR = os.environ.get("TERMINAL_ACTOR", "operator")


def _load_bearer() -> str:
    """Resolve CLOUD_BEARER. Env var wins; falls back to KEYS_DIR/cloud_bearer.txt
    (populated by the handshake helper, `scripts/_init_handshake.py`)."""
    env = os.environ.get("CLOUD_BEARER", "").strip()
    if env:
        return env
    from mailman.terminal.encryption import KEYS_DIR
    bearer_path = KEYS_DIR / "cloud_bearer.txt"
    if bearer_path.exists():
        return bearer_path.read_text().strip()
    return ""


CLOUD_BEARER = _load_bearer()
POLL_INTERVAL_S = int(os.environ.get("SYNC_POLL_INTERVAL_S", "5"))
PUSH_INTERVAL_S = float(os.environ.get("SYNC_PUSH_INTERVAL_S", "2.0"))


def _headers() -> dict:
    if not CLOUD_BEARER:
        # Allow boot without bearer (handshake hasn't run yet); pull/push will fail loudly.
        return {}
    return {"Authorization": f"Bearer {CLOUD_BEARER}"}


# Local sync-state helpers

def _get_cursor() -> int:
    with db.session() as s:
        row = s.execute(
            select(SyncState.value).where(SyncState.key == "last_pulled_event_id")
        ).mappings().first()
        return int(row["value"]) if row else 0


def _set_cursor(event_id: int) -> None:
    with db.session() as s:
        stmt = sqlite_insert(SyncState).values(key="last_pulled_event_id", value=str(event_id),
                                               updated_at=func.datetime("now"))
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=[SyncState.key],
                set_={"value": stmt.excluded.value, "updated_at": func.datetime("now")},
            ).execution_options(synchronize_session=False)
        )
        s.commit()


def _get_unsub_cursor() -> str:
    with db.session() as s:
        row = s.execute(
            select(SyncState.value).where(SyncState.key == "last_pulled_unsub_at")
        ).mappings().first()
        return row["value"] if row else ""


def _set_unsub_cursor(added_at: str) -> None:
    with db.session() as s:
        stmt = sqlite_insert(SyncState).values(key="last_pulled_unsub_at", value=added_at,
                                               updated_at=func.datetime("now"))
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=[SyncState.key],
                set_={"value": stmt.excluded.value, "updated_at": func.datetime("now")},
            ).execution_options(synchronize_session=False)
        )
        s.commit()


def _merge_unsubs(unsubs: list[dict]) -> int:
    """Merge pulled UNSUB# cache rows into suppression_hashes, honoring scope.

    A promotional-scope unsub can be upgraded to 'all' on a later pull; never
    downgraded. reason is always 'unsubscribe' for these.
    """
    if not unsubs:
        return 0
    applied = 0
    with db.session() as s:
        for u in unsubs:
            h = u.get("email_hash")
            if not h:
                continue
            scope = u.get("scope", "all")
            scope = scope if scope in ("promotional", "all") else "all"
            added_at = u.get("added_at") or None
            existing = s.execute(
                select(SuppressionHash.scope).where(SuppressionHash.email_hash == h)
            ).mappings().first()
            if existing is None:
                s.execute(
                    insert(SuppressionHash).values(email_hash=h, reason="unsubscribe", scope=scope,
                                                   source="sync:unsub_pull",
                                                   added_at=func.coalesce(added_at, func.datetime("now")))
                    .execution_options(synchronize_session=False)
                )
                applied += 1
            elif scope == "all" and existing["scope"] != "all":
                # upgrade promotional -> all
                s.execute(
                    update(SuppressionHash).where(SuppressionHash.email_hash == h)
                    .values(scope="all").execution_options(synchronize_session=False)
                )
                applied += 1
        s.commit()
    return applied


def _drain_outbox() -> list[dict]:
    """Pull rows from local outbox awaiting push. Returns wire-format events."""
    with db.session() as s:
        rows = [dict(r) for r in s.execute(
            select(OutboxRow.outbox_id, OutboxRow.table_name, OutboxRow.row_id, OutboxRow.revision,
                   OutboxRow.actor, OutboxRow.modified_at, OutboxRow.key_id, OutboxRow.encrypted_payload,
                   OutboxRow.deleted)
            .where(OutboxRow.pushed_at.is_(None)).order_by(OutboxRow.outbox_id).limit(200)
        ).mappings().all()]
    return rows


def _mark_outbox_pushed(outbox_ids: list[int], assigned_event_ids: list[int]) -> None:
    with db.session() as s:
        for ob_id, ev_id in zip(outbox_ids, assigned_event_ids):
            s.execute(
                update(OutboxRow).where(OutboxRow.outbox_id == ob_id)
                .values(pushed_at=func.datetime("now"), assigned_event_id=ev_id)
                .execution_options(synchronize_session=False)
            )
        s.commit()


# PULL loop

async def _apply_event(event: dict, bundle: encryption.KeyBundle | None) -> None:
    """Decrypt + apply an event to the local DB. Idempotent.

    Store the encrypted blob in `inbox`; the materialiser handles deferred
    decryption and routing to local tables.
    """
    enc_b64 = event.get("encrypted_payload")
    payload_bytes = base64.b64decode(enc_b64) if enc_b64 else None
    with db.session() as s:
        # Idempotent insert keyed on (table_name, row_id, revision) - if we've
        # already seen this exact revision, skip.
        existing = s.execute(
            select(1).select_from(InboxRow)
            .where(InboxRow.table_name == event["table_name"], InboxRow.row_id == event["row_id"],
                   InboxRow.revision == event["revision"])
        ).first()
        if existing:
            return
        s.execute(
            insert(InboxRow).values(event_id=event["event_id"], table_name=event["table_name"],
                                    row_id=event["row_id"], revision=event["revision"], actor=event["actor"],
                                    modified_at=event["modified_at"], key_id=event["key_id"],
                                    encrypted_payload=payload_bytes, deleted=1 if event["deleted"] else 0,
                                    received_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()


async def pull_loop(stop: asyncio.Event) -> None:
    """Short-poll cloud /sync/pull; apply events. Runs forever until stop is set."""
    log.info("Short-poll loop started (cloud=%s, actor=%s)", CLOUD_URL, TERMINAL_ACTOR)
    try:
        bundle = encryption.KeyBundle.load(TERMINAL_ACTOR)
    except encryption.KeyBundleNotReady:
        bundle = None
        log.warning("pull loop: KeyBundle not ready; will pull encrypted blobs to inbox without decrypt")

    backoff_s = 1.0
    while not stop.is_set():
        since = _get_cursor()
        unsub_since = _get_unsub_cursor()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{CLOUD_URL}/sync/pull",
                    params={"since": since, "unsub_since": unsub_since},
                    headers=_headers(),
                )
            if r.status_code == 401:
                log.warning("pull: 401 - bearer not configured; sleeping 30s")
                await asyncio.sleep(30)
                continue
            r.raise_for_status()
            data = r.json()
            events = data.get("events", [])
            if events:
                for evt in events:
                    await _apply_event(evt, bundle)
                _set_cursor(data["max_event_id"])
                log.info("pulled %d events (cursor -> %d)", len(events), data["max_event_id"])
            # Unsub catch-back - bearer-authed, the only trusted path into suppression
            unsubs = data.get("unsubs", [])
            if unsubs:
                n = _merge_unsubs(unsubs)
                _set_unsub_cursor(data.get("max_unsub_at", unsub_since))
                if n:
                    log.info("merged %d unsub(s) into suppression (cursor -> %s)",
                             n, data.get("max_unsub_at", ""))
            # Mark a successful pull so the lock-confirmer can prove a pull ran post-dispatch
            with db.session() as _s:
                _s.execute(
                    sqlite_insert(SyncState)
                    .values(key="last_pull_ok_at", value=func.datetime("now"), updated_at=func.datetime("now"))
                    .on_conflict_do_update(
                        index_elements=[SyncState.key],
                        set_={"value": func.datetime("now"), "updated_at": func.datetime("now")},
                    ).execution_options(synchronize_session=False)
                )
                _s.commit()
            backoff_s = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("pull error: %s; backoff %ss", e, backoff_s)
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 60.0)
        if not stop.is_set():
            await asyncio.sleep(POLL_INTERVAL_S)


# PUSH loop

async def push_loop(stop: asyncio.Event) -> None:
    """Drain outbox to cloud /sync/push periodically."""
    log.info("push loop started")
    while not stop.is_set():
        try:
            await asyncio.sleep(PUSH_INTERVAL_S)
            outbox = _drain_outbox()
            if not outbox:
                continue
            events_for_wire = []
            outbox_ids = []
            for row in outbox:
                events_for_wire.append({
                    "table":             row["table_name"],
                    "row_id":            row["row_id"],
                    "revision":          row["revision"],
                    "actor":             row["actor"],
                    "modified_at":       row["modified_at"],
                    "key_id":            row["key_id"],
                    "encrypted_payload": (
                        base64.b64encode(row["encrypted_payload"]).decode("ascii")
                        if row["encrypted_payload"] else ""
                    ),
                    "deleted":           bool(row["deleted"]),
                })
                outbox_ids.append(row["outbox_id"])
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{CLOUD_URL}/sync/push",
                    json=events_for_wire,
                    headers=_headers(),
                )
            if r.status_code == 401:
                log.warning("push: 401 - bearer not configured; waiting")
                continue
            r.raise_for_status()
            data = r.json()
            assigned = data.get("assigned_event_ids", [])
            _mark_outbox_pushed(outbox_ids, assigned)
            nc = data.get("near_conflicts", [])
            log.info(
                "pushed %d events (assigned %d ids%s)",
                len(events_for_wire), len(assigned),
                f", {len(nc)} near-conflicts" if nc else "",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("push error: %s", e)
            await asyncio.sleep(5)


# Outcomes loop - poll cloud for resolved pack statuses

OUTCOMES_INTERVAL_S = int(os.environ.get("OUTCOMES_POLL_INTERVAL_S", "30"))


def _get_outcome_cursor() -> str:
    with db.session() as s:
        row = s.execute(
            select(SyncState.value).where(SyncState.key == "last_outcome_enqueued_at")
        ).mappings().first()
        return row["value"] if row else ""


def _set_outcome_cursor(enqueued_at: str) -> None:
    with db.session() as s:
        stmt = sqlite_insert(SyncState).values(key="last_outcome_enqueued_at", value=enqueued_at,
                                               updated_at=func.datetime("now"))
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=[SyncState.key],
                set_={"value": stmt.excluded.value, "updated_at": func.datetime("now")},
            ).execution_options(synchronize_session=False)
        )
        s.commit()


def _apply_pack_outcome(pack: dict) -> None:
    """Update dispatched_job + campaign_recipients from a resolved DynamoDB pack."""
    pack_id = pack["pack_id"]
    status  = pack["status"]   # 'sent' | 'failed' | 'stuck_no_key' | 'cancelled' | 'unknown'
    resend_id     = pack.get("resend_message_id") or ""
    failure_reason = pack.get("failure_reason") or ""
    if status == "unknown":
        _apply_unknown_outcome(pack_id, failure_reason)
        return

    job_status = "completed" if status == "sent" else "failed"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with db.session() as s:
        job = s.execute(
            select(DispatchedJob.campaign_id, DispatchedJob.recipient_id).where(DispatchedJob.job_id == pack_id)
        ).mappings().first()
        if not job:
            return

        if job_status == "completed":
            s.execute(
                update(DispatchedJob).where(DispatchedJob.job_id == pack_id)
                .values(status=job_status, completed_at=now_iso, resend_message_id=resend_id)
                .execution_options(synchronize_session=False)
            )
        else:
            s.execute(
                update(DispatchedJob).where(DispatchedJob.job_id == pack_id)
                .values(status=job_status, resend_message_id=resend_id)
                .execution_options(synchronize_session=False)
            )

        cr_status  = "sent" if status == "sent" else "failed"
        s.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == job["campaign_id"],
                   CampaignRecipient.contact_id == job["recipient_id"],
                   CampaignRecipient.status.not_in(("delivered", "opened", "clicked", "bounced", "complained")))
            .values(status=cr_status, message_id=resend_id, failure_reason=failure_reason or None,
                    sent_at=now_iso if status == "sent" else None)
            .execution_options(synchronize_session=False)
        )
        s.commit()

    if status != "sent":
        db.audit(
            actor="system:outcomes-loop",
            action="pack.outcome_failed",
            target=f"pack:{pack_id}",
            details={"status": status, "reason": failure_reason},
        )


UNKNOWN_PREFIX = "Delivery unknown: "


def _apply_unknown_outcome(pack_id: str, reason: str) -> None:
    """the cloud cannot tell whether this pack's mail went out. The recipient keeps 'sent' (as
    _record_dispatch left it) with the reason in failure_reason, and the job keeps 'dispatched', which
    launch._RETRYABLE_JOB excludes, so neither Retry failed nor a launch mails them again."""
    with db.session() as s:
        job = s.execute(
            select(DispatchedJob.campaign_id, DispatchedJob.recipient_id).where(DispatchedJob.job_id == pack_id)
        ).mappings().first()
        if not job:
            return
        s.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == job["campaign_id"],
                   CampaignRecipient.contact_id == job["recipient_id"],
                   CampaignRecipient.status == "sent")
            .values(failure_reason=UNKNOWN_PREFIX + (reason or "no reason recorded"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="system:outcomes-loop", action="pack.outcome_unknown",
             target=f"pack:{pack_id}", details={"status": "unknown", "reason": reason})


async def outcomes_loop(stop: asyncio.Event) -> None:
    """Poll cloud GET /packs/status; write Resend outcomes back to local DB."""
    log.info("outcomes loop started (interval=%ss)", OUTCOMES_INTERVAL_S)
    backoff_s = 5.0
    while not stop.is_set():
        try:
            await asyncio.sleep(OUTCOMES_INTERVAL_S)
            if not CLOUD_BEARER:
                continue
            cursor = _get_outcome_cursor()
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{CLOUD_URL}/packs/status",
                    params={"since_id": cursor, "limit": 200},
                    headers=_headers(),
                )
            if r.status_code == 401:
                log.warning("outcomes: 401 - bearer missing")
                continue
            r.raise_for_status()
            data = r.json()
            packs = data.get("packs", [])
            if packs:
                for p in packs:
                    _apply_pack_outcome(p)
                new_cursor = data.get("max_id", "")
                if new_cursor:
                    _set_outcome_cursor(new_cursor)
                log.info("outcomes: applied %d pack results (cursor->%s)", len(packs), new_cursor)
            backoff_s = 5.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("outcomes error: %s; backoff %ss", e, backoff_s)
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 120.0)


# Inbox materialiser - decrypt inbox rows into real tables

MATERIALISE_INTERVAL_S = int(os.environ.get("MATERIALISE_INTERVAL_S", "10"))
_MATERIALISABLE = {"webhook_event", "suppression"}


def _materialise_inbox_batch(bundle: encryption.KeyBundle) -> int:
    """Decrypt and apply up to 50 unmaterialised inbox rows. Returns count applied.

    'pack' rows are handled by outcomes_loop (direct API) - stamp them here so the
    inbox stays clean and backup gap-checks don't false-positive on them.
    """
    with db.session() as s:
        rows = s.execute(
            select(InboxRow.event_id, InboxRow.table_name, InboxRow.encrypted_payload)
            .where(InboxRow.materialised_at.is_(None), InboxRow.table_name.in_(("webhook_event", "suppression")))
            .order_by(InboxRow.event_id).limit(50)
        ).mappings().all()

    applied = 0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in rows:
        event_id = row["event_id"]
        table_name = row["table_name"]
        payload_bytes = row["encrypted_payload"]

        try:
            if payload_bytes is None:
                _mark_materialised(event_id, now_iso)
                applied += 1
                continue

            plaintext = encryption.decrypt_for_both(payload_bytes, bundle)
            payload = json.loads(plaintext)

            if table_name == "webhook_event":
                _apply_webhook_event(payload, now_iso)
            elif table_name == "suppression":
                _apply_suppression_event(payload, now_iso)

            _mark_materialised(event_id, now_iso)
            applied += 1
        except Exception as e:
            log.warning("materialiser: failed to process event_id=%s table=%s: %s",
                        event_id, table_name, e)

    return applied


def _mark_materialised(event_id: int, now_iso: str) -> None:
    with db.session() as s:
        s.execute(
            update(InboxRow).where(InboxRow.event_id == event_id)
            .values(materialised_at=now_iso).execution_options(synchronize_session=False)
        )
        s.commit()


def _apply_webhook_event(payload: dict, now_iso: str) -> None:
    """Apply a decrypted webhook_event payload to campaign_recipients."""
    event_type = payload.get("event_type", "")
    message_id = payload.get("message_id", "")
    if not message_id:
        return

    with db.session() as s:
        row = s.execute(
            select(CampaignRecipient.id, CampaignRecipient.campaign_id, CampaignRecipient.contact_id,
                   CampaignRecipient.status).where(CampaignRecipient.message_id == message_id)
        ).mappings().first()
        if not row:
            return

        cur_status = row["status"]
        # Guard: don't downgrade from terminal states
        if cur_status in ("bounced", "complained"):
            if event_type not in ("email.bounced", "email.complained"):
                return

        if event_type == "email.opened":
            # Opens are not tracked - bot/MPP noise. Ignore entirely (raw webhook_event
            # still lands in the inbox for forensics, but never touches engagement).
            return
        elif event_type == "email.clicked":
            s.execute(
                update(CampaignRecipient).where(CampaignRecipient.id == row["id"])
                .values(status="clicked", clicked_at=now_iso).execution_options(synchronize_session=False)
            )
        elif event_type == "email.bounced":
            s.execute(
                update(CampaignRecipient).where(CampaignRecipient.id == row["id"])
                .values(status="bounced", bounced_at=now_iso).execution_options(synchronize_session=False)
            )
            # Auto-suppress hard bounces to protect sender reputation
            import hashlib as _hl
            cr, con = aliased(CampaignRecipient), aliased(Contact)
            email = s.execute(select(con.email).select_from(cr)
                              .join(con, con.id == cr.contact_id).where(cr.id == row["id"])).first()
            if email:
                h = _hl.sha256(email[0].lower().encode()).hexdigest()
                s.execute(insert(SuppressionHash).prefix_with("OR IGNORE")
                          .values(email_hash=h, reason="bounce_hard", source="auto:materialiser")
                          .execution_options(synchronize_session=False))
        elif event_type == "email.complained":
            s.execute(
                update(CampaignRecipient).where(CampaignRecipient.id == row["id"])
                .values(status="complained", complained_at=now_iso).execution_options(synchronize_session=False)
            )
        s.commit()


def _apply_suppression_event(payload: dict, now_iso: str) -> None:
    """Apply a decrypted suppression sync event to local suppression_hashes."""
    email_hash = payload.get("email_hash", "")
    reason = payload.get("reason", "unsubscribe")
    source = payload.get("source", "sync")
    if not email_hash:
        return
    with db.session() as s:
        s.execute(
            insert(SuppressionHash).prefix_with("OR IGNORE")
            .values(email_hash=email_hash, reason=reason, source=source, added_at=payload.get("added_at", now_iso))
            .execution_options(synchronize_session=False)
        )
        s.commit()


def _stamp_pack_inbox_rows() -> int:
    """Mark pack-outcome inbox rows as materialised - they're handled by outcomes_loop."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session() as s:
        rows = s.execute(
            select(InboxRow.event_id).where(InboxRow.materialised_at.is_(None), InboxRow.table_name == "pack")
        ).mappings().all()
        if rows:
            for r in rows:
                s.execute(update(InboxRow).where(InboxRow.event_id == r["event_id"])
                          .values(materialised_at=now).execution_options(synchronize_session=False))
            s.commit()
            log.debug("materialiser: stamped %d pack rows as materialised", len(rows))
    return len(rows)


async def materialiser_loop(stop: asyncio.Event) -> None:
    """Decrypt unmaterialised inbox rows into campaign_recipients + suppression_hashes."""
    log.info("materialiser loop started (interval=%ss)", MATERIALISE_INTERVAL_S)
    bundle: encryption.KeyBundle | None = None
    backoff_s = 5.0
    while not stop.is_set():
        try:
            await asyncio.sleep(MATERIALISE_INTERVAL_S)
            # Stamp pack-outcome inbox rows unconditionally - handled by outcomes_loop
            _stamp_pack_inbox_rows()
            if bundle is None:
                try:
                    bundle = encryption.KeyBundle.load(TERMINAL_ACTOR)
                except encryption.KeyBundleNotReady:
                    log.debug("materialiser: KeyBundle not ready, skipping")
                    continue
            n = _materialise_inbox_batch(bundle)
            if n:
                log.info("materialiser: applied %d inbox rows", n)
            backoff_s = 5.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("materialiser error: %s; backoff %ss", e, backoff_s)
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 120.0)


def _drain_batches() -> None:
    """Transition 'sending' batches to 'drained' once their last send_at has passed,
    and set the analytics-cooldown gate (approve_unlock_at = next day's window).
    A drained batch is what unlocks manual approve-next for the following batch."""
    from mailman.terminal import send_schedule, settings as _s
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session() as s:
        rows = s.execute(
            select(CampaignBatch.id, CampaignBatch.window_end)
            .where(CampaignBatch.status == "sending", CampaignBatch.window_end.is_not(None),
                   CampaignBatch.window_end < now_iso)
        ).mappings().all()
        if not rows:
            return
        unlock = send_schedule.next_window_open_after(
            now,
            tz=_s.get("send_window_tz", "America/Los_Angeles") or "America/Los_Angeles",
            start_hour=_s.get_int("send_window_start_hour", 9),
            window_hours=_s.get_int("send_window_hours", 4),
        )
        for r in rows:
            s.execute(
                update(CampaignBatch).where(CampaignBatch.id == r["id"])
                .values(status="drained", drained_at=now_iso, approve_unlock_at=unlock)
                .execution_options(synchronize_session=False)
            )
        s.commit()
        log.info("batch: %d batch(es) drained; approve-next unlocks at %s", len(rows), unlock)


# Scheduler loop - auto-launch scheduled campaigns

SCHEDULER_INTERVAL_S = int(os.environ.get("SCHEDULER_INTERVAL_S", "60"))
_RETRY_NOTE_WINDOW = "-1 day"
_UNPARSEABLE_TIME = "its scheduled time is not a valid date and time"
ARCHIVED_STAGE_REASON = "the campaign was archived before this stage went out"
CANCELLED_STAGE_REASON = "the campaign was cancelled before this stage went out"
_STAGE_CANCEL_REASON = {"archived": ARCHIVED_STAGE_REASON, "cancelled": CANCELLED_STAGE_REASON}
_RETRY_GIVE_UP_S = 24 * 60 * 60
_STREAK_FMT = "%Y-%m-%d %H:%M:%S"


def _still_scheduled(campaign_id: int) -> bool:
    with db.session() as s:
        return s.execute(select(Campaign.status).where(Campaign.id == campaign_id)).scalar_one_or_none() == "scheduled"


def _note_retry(action: str, campaign_id: int, details: dict) -> None:
    """A launch the scheduler will try again is audited once per distinct reason per day,
    not once a tick. It writes no drift row."""
    from mailman.terminal.models import AuditEvent
    target, payload = f"campaign:{campaign_id}", json.dumps(details)
    with db.session() as s:
        seen = s.execute(select(AuditEvent.id).where(
            AuditEvent.action == action, AuditEvent.target == target, AuditEvent.details_json == payload,
            AuditEvent.occurred_at >= func.datetime("now", _RETRY_NOTE_WINDOW)).limit(1)).first()
    if seen is None:
        db.audit(actor="system:scheduler", action=action, target=target, details=details)


def _now() -> datetime:
    """The scheduler's clock for the 24 h refusal rule; tests replace it."""
    return datetime.now(timezone.utc)


def _streak(campaign_id: int, now: datetime) -> tuple[datetime, str | None]:
    """When the campaign's current streak of unexpected launch errors began, and the
    campaigns.updated_at it belongs to. The newest retry row that has a streak_start carries the streak while
    the campaign's updated_at still equals the one stored with it; a successful launch and an operator edit
    both bump updated_at, so either one starts a new streak. Rows without streak_start (LaunchRetryable
    reasons) neither count toward it nor end it. A new streak starts at now."""
    from mailman.terminal.models import AuditEvent
    with db.session() as s:
        since = s.execute(select(Campaign.updated_at).where(Campaign.id == campaign_id)).scalar_one_or_none()
        raws = s.execute(select(AuditEvent.details_json).where(
            AuditEvent.action == "campaign.scheduled_launch_retrying", AuditEvent.target == f"campaign:{campaign_id}")
            .order_by(AuditEvent.id.desc())).scalars().all()
    for raw in raws:
        try:
            d = json.loads(raw)
            begun = datetime.strptime(d["streak_start"], _STREAK_FMT).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, KeyError):
            continue
        return (begun if d.get("updated_at") == since else now), since
    return now, since


def _refuse_campaign(campaign_id: int, error: str, since: str | None = None, unparseable: str | None = None) -> bool:
    """A due campaign launch refused leaves 'scheduled' for 'failed', with one audit row and one
    drift row carrying the reason, in one transaction. The update is guarded by the selection predicate,
    so an operator change made while the scheduler held the campaign wins. Returns True if this call moved it.
    since: the updated_at the 24 h streak belongs to; an operator edit after it wins."""
    from mailman.terminal.models import AuditEvent
    due = ((Campaign.scheduled_for == unparseable,) if unparseable is not None else  # Same unreadable value.
           (Campaign.scheduled_for.is_not(None), func.datetime(Campaign.scheduled_for) <= func.datetime("now")))
    with db.session() as s:
        moved = s.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.status == "scheduled", *due)
            .where(Campaign.updated_at == since if since is not None else True)
            .values(status="failed", updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)).rowcount == 1
        s.execute(insert(AuditEvent)
                  .values(actor="system:scheduler", action="campaign.scheduled_launch_failed",
                          target=f"campaign:{campaign_id}",
                          details_json=json.dumps({"error": error, "marked_failed": moved}))
                  .execution_options(synchronize_session=False))
        s.execute(insert(DriftReport)
                  .values(severity="WARN", category="status_mismatch", job_id=f"sched:{campaign_id}",
                          details_json=json.dumps({"campaign_id": campaign_id, "error": error,
                                                   "marked_failed": moved}),
                          detected_at=func.datetime("now"))
                  .execution_options(synchronize_session=False))
        s.commit()
    return moved


def _fail_stage(campaign_id: int, stage_id: int, error: str, possibly_sent: bool) -> None:
    """A refused stage is marked failed with one audit row and one drift row, in one transaction.
    possibly_sent: the failure came after the push began, or was any unexpected (non-LaunchError) error, so some of its mail may be live."""
    from mailman.terminal.models import AuditEvent
    with db.session() as s:
        s.execute(update(CampaignStage).where(CampaignStage.id == stage_id, CampaignStage.status == "pending")
                  .values(status="failed").execution_options(synchronize_session=False))
        s.execute(insert(AuditEvent)
                  .values(actor="system:scheduler", action="campaign.stage_failed", target=f"campaign:{campaign_id}",
                          details_json=json.dumps({"stage_id": stage_id, "error": error,
                                                   "possibly_sent": possibly_sent}))
                  .execution_options(synchronize_session=False))
        s.execute(insert(DriftReport)
                  .values(severity="WARN", category="status_mismatch", job_id=f"stage:{stage_id}",
                          details_json=json.dumps({"campaign_id": campaign_id, "stage_id": stage_id, "error": error,
                                                   "possibly_sent": possibly_sent}),
                          detected_at=func.datetime("now"))
                  .execution_options(synchronize_session=False))
        s.commit()


def _stage_still_due(stage_id: int) -> bool:
    """Re-read a selected stage right before its launch. An archive (or a pause) pressed while this tick
    was launching earlier stages wins; a launch already under way is not stopped."""
    with db.session() as s:
        return s.execute(
            select(CampaignStage.id).join(Campaign, Campaign.id == CampaignStage.campaign_id)
            .where(CampaignStage.id == stage_id, CampaignStage.status == "pending",
                   Campaign.status.not_in(("paused", "draft", "archived", "cancelled")))
        ).first() is not None


def _cancel_archived_stage(campaign_id: int, stage_id: int, campaign_status: str = "archived") -> bool:
    """A pending stage of an archived campaign leaves 'pending' for 'cancelled' with one audit row carrying
    the reason, in one transaction. Guarded on the stage still pending and the campaign still archived, so an
    operator change made in between wins and nothing is written. Returns True if this call moved it."""
    from mailman.terminal.models import AuditEvent
    reason = _STAGE_CANCEL_REASON[campaign_status]   # KeyError before any write
    with db.session() as s:
        archived = select(Campaign.id).where(Campaign.id == campaign_id, Campaign.status == campaign_status).exists()
        moved = s.execute(
            update(CampaignStage)
            .where(CampaignStage.id == stage_id, CampaignStage.status == "pending", archived)
            .values(status="cancelled")
            .execution_options(synchronize_session=False)).rowcount == 1
        if moved:
            s.execute(insert(AuditEvent)
                      .values(actor="system:scheduler", action="campaign.stage_cancelled",
                              target=f"campaign:{campaign_id}",
                              details_json=json.dumps({"stage_id": stage_id, "reason": reason}))
                      .execution_options(synchronize_session=False))
        s.commit()
    return moved


async def scheduler_tick() -> None:
    """One scheduler pass (split out of scheduler_loop so a tick can be driven directly)."""
    from mailman.terminal import launch as _launch
    # 0. Release elapsed campaign locks
    try:
        from mailman.terminal import campaign_lock
        released = campaign_lock.confirm_elapsed_locks()
        if released:
            log.info("lock: confirmed %d campaign(s), contacts released", released)
    except Exception as e:
        log.warning("lock confirm error: %s", e)

    # 0b. Batch drain -> cooldown (v3.22)
    try:
        _drain_batches()
    except Exception as e:
        log.warning("batch drain error: %s", e)
    # 1. Single-shot scheduled campaigns
    with db.session() as s:
        due = s.execute(
            select(Campaign.id)
            .where(Campaign.status == "scheduled", Campaign.scheduled_for.is_not(None),
                   func.datetime(Campaign.scheduled_for) <= func.datetime("now"))
            .order_by(Campaign.id)
        ).mappings().all()
    for row in due:
        campaign_id = row["id"]
        try:
            await _launch.launch_campaign(campaign_id, actor="system:scheduler")
            log.info("scheduler: launched campaign %d", campaign_id)
        except Exception as exc:
            try:
                reason = str(exc) or type(exc).__name__
                retry = isinstance(exc, _launch.LaunchRetryable) or not isinstance(exc, _launch.LaunchError)
                if retry and _still_scheduled(campaign_id):
                    if isinstance(exc, _launch.LaunchError):
                        _, since = _streak(campaign_id, _now())
                        log.warning("scheduler: campaign %d not launched, retrying next tick: %s", campaign_id, reason)
                        _note_retry("campaign.scheduled_launch_retrying", campaign_id, {"error": reason, "updated_at": since})
                    else:
                        now = _now()
                        begun, since = _streak(campaign_id, now)
                        if (now - begun).total_seconds() >= _RETRY_GIVE_UP_S:
                            log.warning("scheduler: campaign %d refused after a day of unexpected errors: %s",
                                        campaign_id, reason)
                            _refuse_campaign(campaign_id, reason, since)
                        else:
                            log.warning("scheduler: campaign %d not launched, retrying next tick: %s",
                                        campaign_id, reason)
                            _note_retry("campaign.scheduled_launch_retrying", campaign_id,
                                        {"error": reason, "streak_start": begun.strftime(_STREAK_FMT),
                                         "updated_at": since})
                else:
                    log.warning("scheduler: campaign %d refused: %s", campaign_id, reason)
                    _refuse_campaign(campaign_id, reason)
            except Exception as rec:
                log.warning("scheduler: could not record campaign %d: %s", campaign_id, rec)
    # Cancel a pending stage of an archived campaign once, with the reason.
    with db.session() as s:
        archived_stages = s.execute(
            select(CampaignStage.id.label("stage_id"), CampaignStage.campaign_id, Campaign.status.label("campaign_status"))
            .join(Campaign, Campaign.id == CampaignStage.campaign_id)
            .where(CampaignStage.status == "pending", Campaign.status.in_(("archived", "cancelled")))
        ).mappings().all()
    for row in archived_stages:
        try:
            if _cancel_archived_stage(row["campaign_id"], row["stage_id"], row["campaign_status"]):
                log.info("scheduler: stage %d not sent: %s", row["stage_id"], _STAGE_CANCEL_REASON[row["campaign_status"]])
        except Exception as rec:
            log.warning("scheduler: could not record stage %d: %s", row["stage_id"], rec)
    # 2. Multi-stage campaign stages
    with db.session() as s:
        cs, ca = aliased(CampaignStage), aliased(Campaign)
        due_stages = s.execute(
            select(cs.id.label("stage_id"), cs.campaign_id, cs.template_id, cs.stage_number)
            .join(ca, ca.id == cs.campaign_id)
            .where(
                cs.status == "pending",
                func.datetime(cs.scheduled_for) <= func.datetime("now"),
                ca.status.not_in(("paused", "draft", "archived", "cancelled")),
                ca.test_sent_at.is_not(None),
            )
        ).mappings().all()
    for row in due_stages:
        if not _stage_still_due(row["stage_id"]):   # An archive or pause pressed during this tick wins
            continue
        try:
            await _launch.launch_campaign(
                row["campaign_id"], actor="system:scheduler",
                template_id_override=row["template_id"],
                stage_id=row["stage_id"],
            )
            log.info("scheduler: launched stage %d (campaign %d, stage #%d)",
                     row["stage_id"], row["campaign_id"], row["stage_number"])
        except Exception as exc:
            try:
                reason = str(exc) or type(exc).__name__
                if isinstance(exc, _launch.LaunchRetryable):
                    log.warning("scheduler: stage %d not launched, retrying next tick: %s", row["stage_id"], reason)
                    _note_retry("campaign.stage_retrying", row["campaign_id"],
                                {"stage_id": row["stage_id"], "error": reason})
                else:
                    sent = isinstance(exc, _launch.LaunchPushError) or not isinstance(exc, _launch.LaunchError)
                    log.warning("scheduler: stage %d failed: %s", row["stage_id"], reason)
                    _fail_stage(row["campaign_id"], row["stage_id"], reason, sent)
            except Exception as rec:
                log.warning("scheduler: could not record stage %d: %s", row["stage_id"], rec)
    # 3. A stored time SQLite cannot read never comes due; fail it once, with the reason.
    with db.session() as s:
        bad_campaigns = s.execute(
            select(Campaign.id, Campaign.scheduled_for)
            .where(Campaign.status == "scheduled", Campaign.scheduled_for.is_not(None),
                   func.datetime(Campaign.scheduled_for).is_(None))
        ).mappings().all()
        cs, ca = aliased(CampaignStage), aliased(Campaign)
        bad_stages = s.execute(
            select(cs.id.label("stage_id"), cs.campaign_id)
            .join(ca, ca.id == cs.campaign_id)
            .where(cs.status == "pending", func.datetime(cs.scheduled_for).is_(None),
                   ca.status.not_in(("paused", "draft", "archived", "cancelled")), ca.test_sent_at.is_not(None))
        ).mappings().all()
    for row in bad_campaigns:
        try:
            log.warning("scheduler: campaign %d refused: %s", row["id"], _UNPARSEABLE_TIME)
            _refuse_campaign(row["id"], _UNPARSEABLE_TIME, unparseable=row["scheduled_for"])
        except Exception as rec:
            log.warning("scheduler: could not record campaign %d: %s", row["id"], rec)
    for row in bad_stages:
        try:
            log.warning("scheduler: stage %d failed: %s", row["stage_id"], _UNPARSEABLE_TIME)
            _fail_stage(row["campaign_id"], row["stage_id"], _UNPARSEABLE_TIME, possibly_sent=False)
        except Exception as rec:
            log.warning("scheduler: could not record stage %d: %s", row["stage_id"], rec)


async def scheduler_loop(stop: asyncio.Event) -> None:
    """Fire scheduled campaigns + pending stages when their time has passed."""
    log.info("scheduler loop started (interval=%ss)", SCHEDULER_INTERVAL_S)
    while not stop.is_set():
        try:
            await asyncio.sleep(SCHEDULER_INTERVAL_S)
            await scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("scheduler error: %s", e)


# Local enqueue helper (called by UI/CRUD code)

def enqueue_local_write(
    table: str, row_id: str, revision: int, modified_at: str,
    key_id: str, encrypted_payload: bytes | None, deleted: bool = False,
) -> None:
    """The CRUD path calls this after writing locally. The push loop will
    pick it up + ship to cloud.
    """
    with db.session() as s:
        s.execute(
            insert(OutboxRow).values(table_name=table, row_id=row_id, revision=revision, actor=TERMINAL_ACTOR,
                                     modified_at=modified_at, key_id=key_id, encrypted_payload=encrypted_payload,
                                     deleted=1 if deleted else 0)
            .execution_options(synchronize_session=False)
        )
        s.commit()
