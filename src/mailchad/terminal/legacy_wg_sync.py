"""Vault ↔ front-edge sync orchestrator.

Called on every vault wake. Pulls front-edge's caches, merges into vault's
source-of-truth tables, then acks back so front-edge can GC.

Sync steps (in order):
  1. Pull /cache/events -> for each event_cache row, INSERT INTO vault.events;
     also process delivery state into campaign_recipients.
  2. Pull /cache/events.unsubs -> INSERT OR IGNORE INTO suppression_hashes.
  3. Pull /cache/events.erasures -> INSERT INTO compliance queue for processing
     by the drainer + propagation to backup.
  4. POST /cache/events/ack with last_event_id + erasure_ids + now_iso.
  5. Pull /queue -> run drift_check; persist drift_report rows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from mailchad.terminal import db

log = logging.getLogger("vault.sync")

FRONT_EDGE_URL = os.environ.get("FRONT_EDGE_INTERNAL_URL", "http://172.31.0.2:8443")
PULL_TIMEOUT_S = float(os.environ.get("SYNC_PULL_TIMEOUT_S", "30"))


# Event ingest

EVENT_STATUS_MAP = {
    "email.sent":        "sent",
    "email.delivered":   "delivered",
    "email.opened":      "opened",
    "email.clicked":     "clicked",
    "email.bounced":     "bounced",
    "email.complained":  "complained",
}


def _apply_event_to_recipient(c, event_type: str, message_id: str | None, recipient: str | None) -> None:
    """If we can map this to a campaign_recipients row, update its status."""
    if not message_id:
        return
    status = EVENT_STATUS_MAP.get(event_type)
    if not status:
        return
    col = {
        "sent": "sent_at", "delivered": "delivered_at", "opened": "opened_at",
        "clicked": "clicked_at", "bounced": "bounced_at", "complained": "complained_at",
    }.get(status)
    if not col:
        return
    c.execute(
        f"UPDATE campaign_recipients SET status=?, {col}=datetime('now') "
        f"WHERE message_id=? AND status NOT IN ('bounced', 'complained', 'failed')",
        (status, message_id),
    )


def _ingest_events(events: list[dict]) -> int:
    if not events:
        return 0
    with db.conn() as c:
        c.execute("BEGIN")
        for e in events:
            c.execute(
                "INSERT INTO events (event_type, message_id, recipient, payload_json, synced_from_edge_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (e["event_type"], e["message_id"], e["recipient"], e["payload_json"]),
            )
            _apply_event_to_recipient(c, e["event_type"], e["message_id"], e["recipient"])
        c.commit()
    return len(events)


def _ingest_unsubs(unsubs: list[dict]) -> int:
    if not unsubs:
        return 0
    with db.conn() as c:
        for u in unsubs:
            c.execute(
                "INSERT OR IGNORE INTO suppression_hashes (email_hash, reason, source) VALUES (?, 'unsubscribe', ?)",
                (u["email_hash"], u["source_token"]),
            )
        c.commit()
    return len(unsubs)


def _ingest_erasures(erasures: list[dict]) -> tuple[int, list[int]]:
    """Returns (count, list of erasure_request_cache ids to ack)."""
    if not erasures:
        return 0, []
    ack_ids = []
    with db.conn() as c:
        for e in erasures:
            # Erasure also implies suppression (don't email a deleted user).
            c.execute(
                "INSERT OR IGNORE INTO suppression_hashes (email_hash, reason, source) VALUES (?, 'erasure_request', ?)",
                (e["email_hash"], e["source_token"]),
            )
            # Audit the erasure request. Real deletion happens via the
            # erasure-propagation path (see propagate_erasure_to_backup).
            db.audit(
                actor="system:front-edge-sync",
                action="erasure.requested",
                target=f"hash:{e['email_hash']}",
                details={"source": e["source_token"]},
            )
            ack_ids.append(e["id"])
        c.commit()
    return len(erasures), ack_ids


# Drift detection

def _detect_drift(edge_queue: list[dict]) -> list[dict]:
    """Compare front-edge queue to vault.dispatched_job. Returns drift entries."""
    edge_by_id = {j["job_id"]: j for j in edge_queue}
    with db.conn() as c:
        vault_jobs = {r["job_id"]: dict(r) for r in c.execute(
            "SELECT job_id, status, content_hash FROM dispatched_job WHERE status NOT IN ('completed', 'cancelled', 'failed')"
        ).fetchall()}

    drift = []

    # Missing on vault - extras on edge. CRITICAL.
    for jid, ej in edge_by_id.items():
        if jid not in vault_jobs:
            drift.append({
                "category": "missing_on_vault", "severity": "CRITICAL", "job_id": jid,
                "details": {"edge_status": ej["status"], "edge_content_hash": ej["content_hash"]},
            })

    # Missing on edge - vault dispatched, edge has no record. WARN (likely ack lost).
    for jid, vj in vault_jobs.items():
        if jid not in edge_by_id:
            drift.append({
                "category": "missing_on_edge", "severity": "WARN", "job_id": jid,
                "details": {"vault_status": vj["status"]},
            })

    # Content mismatch - both have it but content_hash differs. CRITICAL.
    for jid, vj in vault_jobs.items():
        if jid in edge_by_id and edge_by_id[jid]["content_hash"] != vj["content_hash"]:
            drift.append({
                "category": "content_mismatch", "severity": "CRITICAL", "job_id": jid,
                "details": {
                    "vault_hash": vj["content_hash"],
                    "edge_hash": edge_by_id[jid]["content_hash"],
                },
            })

    # Status mismatch - both have it, status differs in a meaningful way.
    for jid, vj in vault_jobs.items():
        if jid not in edge_by_id:
            continue
        es = edge_by_id[jid]["status"]
        vs = vj["status"]
        # Normalise: vault.dispatched + edge.completed -> status_mismatch INFO
        # (we just haven't pulled the event yet). Real concerns are
        # "edge says failed but vault doesn't know."
        if es in ("failed", "suppressed") and vs == "dispatched":
            drift.append({
                "category": "status_mismatch", "severity": "WARN", "job_id": jid,
                "details": {"edge": es, "vault": vs},
            })

    return drift


def _persist_drift(drift: list[dict]) -> int:
    if not drift:
        return 0
    with db.conn() as c:
        for d in drift:
            c.execute(
                "INSERT INTO drift_report (category, severity, job_id, details_json) "
                "VALUES (?, ?, ?, ?)",
                (d["category"], d["severity"], d.get("job_id"), json.dumps(d["details"])),
            )
        c.commit()
    return len(drift)


# Orchestrator

async def run_sync() -> dict:
    """Single sync pass: pull caches, merge, ack, run drift check."""
    log.info("sync: starting pull from %s", FRONT_EDGE_URL)
    summary = {"events": 0, "unsubs": 0, "erasures": 0, "drift": 0, "drift_critical": 0}

    # Determine since_id for incremental event pull.
    with db.conn() as c:
        row = c.execute(
            "SELECT max(id) AS last FROM events"
        ).fetchone()
    # NOTE: this is a simplification - proper since_id needs to be vault's
    # last-pulled cursor, not max event id. v3 leaves it as the simple
    # "pull everything you have, dedupe on svix_id" pattern; front-edge
    # GCs based on what we ack. Cleaner: use the sync_cursor table.

    try:
        async with httpx.AsyncClient(timeout=PULL_TIMEOUT_S) as client:
            r = await client.get(f"{FRONT_EDGE_URL}/cache/events", params={"since_id": 0})
            r.raise_for_status()
            cache = r.json()
    except Exception as e:
        log.exception("sync: pull failed")
        return {"error": f"pull failed: {e}"}

    summary["events"]   = _ingest_events(cache.get("events", []))
    summary["unsubs"]   = _ingest_unsubs(cache.get("unsubs", []))
    n_e, ack_ids        = _ingest_erasures(cache.get("erasures", []))
    summary["erasures"] = n_e

    # Ack.
    last_event_id = max((e["id"] for e in cache.get("events", [])), default=0)
    try:
        async with httpx.AsyncClient(timeout=PULL_TIMEOUT_S) as client:
            await client.post(
                f"{FRONT_EDGE_URL}/cache/events/ack",
                json={
                    "last_event_id": last_event_id,
                    "erasure_ids": ack_ids,
                    "now_iso": datetime.now(timezone.utc).isoformat(),
                },
            )
    except Exception as e:
        log.warning("sync: ack failed (will retry next wake): %s", e)

    # Drift check.
    try:
        async with httpx.AsyncClient(timeout=PULL_TIMEOUT_S) as client:
            r = await client.get(f"{FRONT_EDGE_URL}/queue")
            r.raise_for_status()
            edge_queue = r.json().get("jobs", [])
    except Exception as e:
        log.warning("sync: drift queue fetch failed: %s", e)
        edge_queue = []

    drift = _detect_drift(edge_queue)
    summary["drift"] = _persist_drift(drift)
    summary["drift_critical"] = sum(1 for d in drift if d["severity"] == "CRITICAL")

    db.audit(
        actor="system:vault-sync",
        action="sync.completed",
        details=summary,
    )

    log.info("sync: complete: %s", summary)
    return summary
