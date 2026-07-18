"""Terminal-side sync client - short-poll loop + push (spec §3.3 + §13.4).

Runs as a background asyncio task. Two responsibilities:

  1. PULL - short-poll cloud /sync/pull?since=N -> receive events past cursor ->
     decrypt with KeyBundle -> apply to local DB -> advance cursor.

  2. PUSH - drain local outbox table -> POST /sync/push -> on success,
     mark outbox rows as sent.

Idempotent + resumable: cursor stored in local DB. Reconnect = replay
from cursor. Events with revision <= local row's revision no-op (last-
write-wins via Lamport, §3.2).

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

from mailchad.terminal import db, encryption

log = logging.getLogger("terminal.sync")

CLOUD_URL = os.environ.get("CLOUD_URL", "http://cloud:8443")
TERMINAL_ACTOR = os.environ.get("TERMINAL_ACTOR", "operator")


def _load_bearer() -> str:
    """Resolve CLOUD_BEARER. Env var wins; falls back to KEYS_DIR/cloud_bearer.txt
    (populated by `bin/v3 init-handshake`)."""
    env = os.environ.get("CLOUD_BEARER", "").strip()
    if env:
        return env
    from mailchad.terminal.encryption import KEYS_DIR
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
    with db.conn() as c:
        row = c.execute(
            "SELECT value FROM sync_state WHERE key = 'last_pulled_event_id'"
        ).fetchone()
        return int(row["value"]) if row else 0


def _set_cursor(event_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES "
            "('last_pulled_event_id', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (str(event_id),),
        )
        c.commit()


def _get_unsub_cursor() -> str:
    with db.conn() as c:
        row = c.execute(
            "SELECT value FROM sync_state WHERE key = 'last_pulled_unsub_at'"
        ).fetchone()
        return row["value"] if row else ""


def _set_unsub_cursor(added_at: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES "
            "('last_pulled_unsub_at', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (added_at,),
        )
        c.commit()


def _merge_unsubs(unsubs: list[dict]) -> int:
    """Merge pulled UNSUB# cache rows into suppression_hashes, honoring scope.

    A promotional-scope unsub can be upgraded to 'all' on a later pull; never
    downgraded. reason is always 'unsubscribe' for these.
    """
    if not unsubs:
        return 0
    applied = 0
    with db.conn() as c:
        for u in unsubs:
            h = u.get("email_hash")
            if not h:
                continue
            scope = u.get("scope", "all")
            scope = scope if scope in ("promotional", "all") else "all"
            added_at = u.get("added_at") or None
            existing = c.execute(
                "SELECT scope FROM suppression_hashes WHERE email_hash = ?", (h,)
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO suppression_hashes (email_hash, reason, scope, source, added_at) "
                    "VALUES (?, 'unsubscribe', ?, 'sync:unsub_pull', COALESCE(?, datetime('now')))",
                    (h, scope, added_at),
                )
                applied += 1
            elif scope == "all" and existing["scope"] != "all":
                # upgrade promotional -> all
                c.execute(
                    "UPDATE suppression_hashes SET scope = 'all' WHERE email_hash = ?", (h,)
                )
                applied += 1
        c.commit()
    return applied


def _drain_outbox() -> list[dict]:
    """Pull rows from local outbox awaiting push. Returns wire-format events."""
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT outbox_id, table_name, row_id, revision, actor, modified_at, "
            "key_id, encrypted_payload, deleted "
            "FROM outbox WHERE pushed_at IS NULL ORDER BY outbox_id LIMIT 200"
        ).fetchall()]
    return rows


def _mark_outbox_pushed(outbox_ids: list[int], assigned_event_ids: list[int]) -> None:
    with db.conn() as c:
        for ob_id, ev_id in zip(outbox_ids, assigned_event_ids):
            c.execute(
                "UPDATE outbox SET pushed_at = datetime('now'), assigned_event_id = ? "
                "WHERE outbox_id = ?",
                (ev_id, ob_id),
            )
        c.commit()


# PULL loop

async def _apply_event(event: dict, bundle: encryption.KeyBundle | None) -> None:
    """Decrypt + apply an event to the local DB. Idempotent.

    Strategy: store the encrypted blob in `inbox` for now; deferred
    decryption + table-routing is the next layer (admin UI reads inbox
    when displaying things, OR a separate worker materialises into the
    real tables - pick once we wire the UI in §13).
    """
    enc_b64 = event.get("encrypted_payload")
    payload_bytes = base64.b64decode(enc_b64) if enc_b64 else None
    with db.conn() as c:
        # Idempotent insert keyed on (table_name, row_id, revision) - if we've
        # already seen this exact revision, skip.
        existing = c.execute(
            "SELECT 1 FROM inbox WHERE table_name = ? AND row_id = ? AND revision = ?",
            (event["table_name"], event["row_id"], event["revision"]),
        ).fetchone()
        if existing:
            return
        c.execute(
            "INSERT INTO inbox (event_id, table_name, row_id, revision, actor, "
            "modified_at, key_id, encrypted_payload, deleted, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (event["event_id"], event["table_name"], event["row_id"],
             event["revision"], event["actor"], event["modified_at"],
             event["key_id"], payload_bytes, 1 if event["deleted"] else 0),
        )
        c.commit()


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
            with db.conn() as _c:
                _c.execute(
                    "INSERT INTO sync_state (key, value, updated_at) VALUES "
                    "('last_pull_ok_at', datetime('now'), datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value=datetime('now'), updated_at=datetime('now')"
                )
                _c.commit()
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
    with db.conn() as c:
        row = c.execute(
            "SELECT value FROM sync_state WHERE key = 'last_outcome_enqueued_at'"
        ).fetchone()
        return row["value"] if row else ""


def _set_outcome_cursor(enqueued_at: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES "
            "('last_outcome_enqueued_at', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (enqueued_at,),
        )
        c.commit()


def _apply_pack_outcome(pack: dict) -> None:
    """Update dispatched_job + campaign_recipients from a resolved DynamoDB pack."""
    pack_id = pack["pack_id"]
    status  = pack["status"]   # 'sent' | 'failed' | 'stuck_no_key' | 'cancelled'
    resend_id     = pack.get("resend_message_id") or ""
    failure_reason = pack.get("failure_reason") or ""

    job_status = "completed" if status == "sent" else "failed"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with db.conn() as c:
        job = c.execute(
            "SELECT campaign_id, recipient_id FROM dispatched_job WHERE job_id = ?",
            (pack_id,),
        ).fetchone()
        if not job:
            return

        if job_status == "completed":
            c.execute(
                "UPDATE dispatched_job SET status = ?, completed_at = ?, resend_message_id = ? WHERE job_id = ?",
                (job_status, now_iso, resend_id, pack_id),
            )
        else:
            c.execute(
                "UPDATE dispatched_job SET status = ?, resend_message_id = ? WHERE job_id = ?",
                (job_status, resend_id, pack_id),
            )

        cr_status  = "sent" if status == "sent" else "failed"
        c.execute(
            "UPDATE campaign_recipients SET status = ?, message_id = ?, "
            "failure_reason = ?, sent_at = ? "
            "WHERE campaign_id = ? AND contact_id = ? AND status NOT IN ('delivered','opened','clicked','bounced','complained')",
            (cr_status, resend_id, failure_reason or None, now_iso if status == "sent" else None,
             job["campaign_id"], job["recipient_id"]),
        )
        c.commit()

    if status != "sent":
        db.audit(
            actor="system:outcomes-loop",
            action="pack.outcome_failed",
            target=f"pack:{pack_id}",
            details={"status": status, "reason": failure_reason},
        )


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
    with db.conn() as c:
        rows = c.execute(
            "SELECT event_id, table_name, encrypted_payload FROM inbox "
            "WHERE materialised_at IS NULL AND table_name IN ('webhook_event', 'suppression') "
            "ORDER BY event_id LIMIT 50"
        ).fetchall()

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
    with db.conn() as c:
        c.execute(
            "UPDATE inbox SET materialised_at = ? WHERE event_id = ?",
            (now_iso, event_id),
        )
        c.commit()


def _apply_webhook_event(payload: dict, now_iso: str) -> None:
    """Apply a decrypted webhook_event payload to campaign_recipients."""
    event_type = payload.get("event_type", "")
    message_id = payload.get("message_id", "")
    if not message_id:
        return

    with db.conn() as c:
        row = c.execute(
            "SELECT id, campaign_id, contact_id, status FROM campaign_recipients WHERE message_id = ?",
            (message_id,),
        ).fetchone()
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
            c.execute(
                "UPDATE campaign_recipients SET status='clicked', clicked_at=? WHERE id=?",
                (now_iso, row["id"]),
            )
        elif event_type == "email.bounced":
            c.execute(
                "UPDATE campaign_recipients SET status='bounced', bounced_at=? WHERE id=?",
                (now_iso, row["id"]),
            )
            # Auto-suppress hard bounces to protect sender reputation
            import hashlib as _hl
            email = c.execute("SELECT con.email FROM campaign_recipients cr "
                              "JOIN contacts con ON con.id=cr.contact_id WHERE cr.id=?",
                              (row["id"],)).fetchone()
            if email:
                h = _hl.sha256(email[0].lower().encode()).hexdigest()
                c.execute("INSERT OR IGNORE INTO suppression_hashes (email_hash, reason, source) VALUES (?,?,?)",
                          (h, "bounce_hard", "auto:materialiser"))
        elif event_type == "email.complained":
            c.execute(
                "UPDATE campaign_recipients SET status='complained', complained_at=? WHERE id=?",
                (now_iso, row["id"]),
            )
        c.commit()


def _apply_suppression_event(payload: dict, now_iso: str) -> None:
    """Apply a decrypted suppression sync event to local suppression_hashes."""
    email_hash = payload.get("email_hash", "")
    reason = payload.get("reason", "unsubscribe")
    source = payload.get("source", "sync")
    if not email_hash:
        return
    with db.conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO suppression_hashes (email_hash, reason, source, added_at) VALUES (?, ?, ?, ?)",
            (email_hash, reason, source, payload.get("added_at", now_iso)),
        )
        c.commit()


def _stamp_pack_inbox_rows() -> int:
    """Mark pack-outcome inbox rows as materialised - they're handled by outcomes_loop."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.conn() as c:
        rows = c.execute(
            "SELECT event_id FROM inbox WHERE materialised_at IS NULL AND table_name = 'pack'"
        ).fetchall()
        if rows:
            for r in rows:
                c.execute("UPDATE inbox SET materialised_at = ? WHERE event_id = ?", (now, r["event_id"]))
            c.commit()
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
    from mailchad.terminal import send_schedule, settings as _s
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, window_end FROM campaign_batches "
            "WHERE status='sending' AND window_end IS NOT NULL AND window_end < ?",
            (now_iso,),
        ).fetchall()
        if not rows:
            return
        unlock = send_schedule.next_window_open_after(
            now,
            tz=_s.get("send_window_tz", "America/Los_Angeles") or "America/Los_Angeles",
            start_hour=_s.get_int("send_window_start_hour", 9),
            window_hours=_s.get_int("send_window_hours", 4),
        )
        for r in rows:
            c.execute(
                "UPDATE campaign_batches SET status='drained', drained_at=?, "
                "approve_unlock_at=? WHERE id=?",
                (now_iso, unlock, r["id"]),
            )
        c.commit()
        log.info("batch: %d batch(es) drained; approve-next unlocks at %s", len(rows), unlock)


# Scheduler loop - auto-launch scheduled campaigns

SCHEDULER_INTERVAL_S = int(os.environ.get("SCHEDULER_INTERVAL_S", "60"))


async def scheduler_loop(stop: asyncio.Event) -> None:
    """Fire scheduled campaigns + pending stages when their time has passed."""
    log.info("scheduler loop started (interval=%ss)", SCHEDULER_INTERVAL_S)
    while not stop.is_set():
        try:
            await asyncio.sleep(SCHEDULER_INTERVAL_S)
            from mailchad.terminal import launch as _launch

            # 0. Release elapsed campaign locks (Option B: window + pull)
            try:
                from mailchad.terminal import campaign_lock
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
            with db.conn() as c:
                due = c.execute(
                    "SELECT id FROM campaigns WHERE status='scheduled' "
                    "AND scheduled_for IS NOT NULL AND scheduled_for <= datetime('now')"
                ).fetchall()
            for row in due:
                campaign_id = row["id"]
                try:
                    await _launch.launch_campaign(campaign_id, actor="system:scheduler")
                    log.info("scheduler: launched campaign %d", campaign_id)
                except Exception as exc:
                    log.warning("scheduler: failed to launch campaign %d: %s", campaign_id, exc)
                    db.audit(actor="system:scheduler", action="campaign.scheduled_launch_failed",
                             target=f"campaign:{campaign_id}", details={"error": str(exc)})
                    with db.conn() as c:
                        c.execute("INSERT INTO drift_report (severity, category, job_id, detected_at) "
                                  "VALUES ('WARN', 'status_mismatch', ?, datetime('now'))",
                                  (f"sched:{campaign_id}",))
                        c.commit()

            # 2. Multi-stage campaign stages
            with db.conn() as c:
                due_stages = c.execute(
                    "SELECT cs.id AS stage_id, cs.campaign_id, cs.template_id, cs.stage_number "
                    "FROM campaign_stages cs "
                    "JOIN campaigns ca ON ca.id = cs.campaign_id "
                    "WHERE cs.status = 'pending' "
                    "AND cs.scheduled_for <= datetime('now') "
                    "AND ca.status NOT IN ('paused', 'draft') "
                    "AND ca.test_sent_at IS NOT NULL"
                ).fetchall()
            for row in due_stages:
                try:
                    await _launch.launch_campaign(
                        row["campaign_id"], actor="system:scheduler",
                        template_id_override=row["template_id"],
                        stage_id=row["stage_id"],
                    )
                    log.info("scheduler: launched stage %d (campaign %d, stage #%d)",
                             row["stage_id"], row["campaign_id"], row["stage_number"])
                except Exception as exc:
                    log.warning("scheduler: stage %d failed: %s", row["stage_id"], exc)
                    with db.conn() as c:
                        c.execute("UPDATE campaign_stages SET status='failed' WHERE id=?",
                                  (row["stage_id"],))
                        c.commit()
                    db.audit(actor="system:scheduler", action="campaign.stage_failed",
                             target=f"campaign:{row['campaign_id']}", details={"stage_id": row["stage_id"], "error": str(exc)})
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
    with db.conn() as c:
        c.execute(
            "INSERT INTO outbox (table_name, row_id, revision, actor, modified_at, "
            "key_id, encrypted_payload, deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (table, row_id, revision, TERMINAL_ACTOR, modified_at,
             key_id, encrypted_payload, 1 if deleted else 0),
        )
        c.commit()
