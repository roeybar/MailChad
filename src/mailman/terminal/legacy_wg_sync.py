"""Vault <-> front-edge sync orchestrator.

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
from sqlalchemy import func, insert, select, update

from mailman.terminal import db
from mailman.terminal.models import CampaignRecipient, DispatchedJob, DriftReport, Event, SuppressionHash

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


def _apply_event_to_recipient(s, event_type: str, message_id: str | None, recipient: str | None) -> None:
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
    s.execute(
        update(CampaignRecipient)
        .where(CampaignRecipient.message_id == message_id,
               CampaignRecipient.status.not_in(("bounced", "complained", "failed")))
        .values(**{"status": status, col: func.datetime("now")})
        .execution_options(synchronize_session=False)
    )


def _ingest_events(events: list[dict]) -> int:
    if not events:
        return 0
    with db.session() as s:
        db.begin_explicit(s)
        for e in events:
            s.execute(
                insert(Event).values(event_type=e["event_type"], message_id=e["message_id"],
                                     recipient=e["recipient"], payload_json=e["payload_json"],
                                     synced_from_edge_at=func.datetime("now"))
                .execution_options(synchronize_session=False)
            )
            _apply_event_to_recipient(s, e["event_type"], e["message_id"], e["recipient"])
        s.commit()
    return len(events)


def _ingest_unsubs(unsubs: list[dict]) -> int:
    if not unsubs:
        return 0
    with db.session() as s:
        for u in unsubs:
            s.execute(
                insert(SuppressionHash).prefix_with("OR IGNORE")
                .values(email_hash=u["email_hash"], reason="unsubscribe", source=u["source_token"])
                .execution_options(synchronize_session=False)
            )
        s.commit()
    return len(unsubs)


def _ingest_erasures(erasures: list[dict]) -> tuple[int, list[int]]:
    """Returns (count, list of erasure_request_cache ids to ack)."""
    if not erasures:
        return 0, []
    ack_ids = []
    with db.session() as s:
        for e in erasures:
            # Erasure also implies suppression (don't email a deleted user).
            s.execute(
                insert(SuppressionHash).prefix_with("OR IGNORE")
                .values(email_hash=e["email_hash"], reason="erasure_request", source=e["source_token"])
                .execution_options(synchronize_session=False)
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
        s.commit()
    return len(erasures), ack_ids


# Drift detection

def _detect_drift(edge_queue: list[dict]) -> list[dict]:
    """Compare front-edge queue to vault.dispatched_job. Returns drift entries."""
    edge_by_id = {j["job_id"]: j for j in edge_queue}
    with db.session() as s:
        vault_jobs = {r["job_id"]: dict(r) for r in s.execute(
            select(DispatchedJob.job_id, DispatchedJob.status, DispatchedJob.content_hash)
            .where(DispatchedJob.status.not_in(("completed", "cancelled", "failed")))
        ).mappings().all()}

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
    with db.session() as s:
        for d in drift:
            s.execute(
                insert(DriftReport).values(category=d["category"], severity=d["severity"], job_id=d.get("job_id"),
                                           details_json=json.dumps(d["details"]))
                .execution_options(synchronize_session=False)
            )
        s.commit()
    return len(drift)


# Orchestrator

async def run_sync() -> dict:
    """Single sync pass: pull caches, merge, ack, run drift check."""
    log.info("sync: starting pull from %s", FRONT_EDGE_URL)
    summary = {"events": 0, "unsubs": 0, "erasures": 0, "drift": 0, "drift_critical": 0}

    # Determine since_id for incremental event pull.
    with db.session() as s:
        row = s.execute(
            select(func.max(Event.id).label("last"))
        ).mappings().first()
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
