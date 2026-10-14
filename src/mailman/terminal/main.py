"""
terminal - v3.8 local admin UI + sync client.

Runs on each party's laptop. The container server binds 0.0.0.0:8000;
production compose publishes port 8011 on all host interfaces.

Spec sections wired:
  section 3.2   sync_client.pull_loop + push_loop + outcomes_loop background tasks
  section 6     bearer-authed cloud client
  section 3     sync protocol implementation
  section 4     pack lifecycle - launch.py builds/encrypts/pushes packs to /packs
  section 7     init-handshake - scripts/_init_handshake.py helper
  section 9     backup - scripts/_backup.py helper
"""
from __future__ import annotations

from mailman import __version__

import asyncio
import logging
import os

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from mailman.terminal import db, routes_admin, auth, admin_ui, sync_client, launch, settings, settings_api, admin_settings_ui, brand
from mailman.terminal.auth import require_session
from mailman.terminal.models import (AuditEvent, Campaign, Contact, Entity, InboxRow, Operator, OutboxRow,
                                      SuppressionHash, SyncState, Template, cols)


def _migrate_admin_to_operators() -> None:
    """One-time migration: copy settings admin creds into operators table if table is empty."""
    try:
        with db.session() as s:
            count = s.execute(select(func.count().label("n")).select_from(Operator)).mappings().first()
            if count and count["n"] > 0:
                return
            admin_email = settings.get("admin_email", "") or ""
            admin_hash  = settings.get("admin_password_hash", "") or ""
            if admin_email and admin_hash:
                s.execute(
                    insert(Operator).prefix_with("OR IGNORE")
                    .values(email=admin_email.strip().lower(), password_hash=admin_hash, role="admin")
                    .execution_options(synchronize_session=False)
                )
                s.commit()
                log.info("migrated admin credentials from settings to operators table")
    except Exception as e:
        log.warning("operator migration failed (non-fatal): %s", e)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("terminal")

app = FastAPI(title="ep-v3-terminal", version=__version__)
app.include_router(auth.router)
app.include_router(admin_ui.router)
app.include_router(routes_admin.router)
app.include_router(settings_api.router)
app.include_router(admin_settings_ui.router)

TERMINAL_ACTOR = os.environ.get("TERMINAL_ACTOR", "operator")


@app.on_event("startup")
async def on_startup() -> None:
    db.init()
    _migrate_admin_to_operators()
    migrated = settings.migrate_from_env()
    if migrated:
        log.info("settings: migrated %d env vars to DB: %s",
                 len(migrated), ", ".join(migrated.keys()))
    db.audit(actor=f"system:terminal-{TERMINAL_ACTOR}", action="terminal.started")

    # Start sync background tasks (per section 3).
    # They no-op cleanly until the init handshake has populated CLOUD_BEARER.
    stop = asyncio.Event()
    app.state.sync_stop = stop
    app.state.pull_task        = asyncio.create_task(sync_client.pull_loop(stop))
    app.state.push_task        = asyncio.create_task(sync_client.push_loop(stop))
    app.state.outcomes_task    = asyncio.create_task(sync_client.outcomes_loop(stop))
    app.state.materialiser_task = asyncio.create_task(sync_client.materialiser_loop(stop))
    app.state.scheduler_task    = asyncio.create_task(sync_client.scheduler_loop(stop))
    log.info("terminal up; sync+outcomes+materialiser+scheduler loops running (actor=%s)", TERMINAL_ACTOR)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    stop = getattr(app.state, "sync_stop", None)
    if stop:
        stop.set()
    for attr in ("pull_task", "push_task", "outcomes_task", "materialiser_task", "scheduler_task"):
        t = getattr(app.state, attr, None)
        if t:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    db.audit(actor=f"system:terminal-{TERMINAL_ACTOR}", action="terminal.stopped")


@app.get("/healthz")
async def healthz():
    with db.session() as s:
        counts = {
            "contacts":  s.execute(select(func.count().label("n")).select_from(Contact)).mappings().first()["n"],
            "campaigns": s.execute(select(func.count().label("n")).select_from(Campaign)).mappings().first()["n"],
            "suppression_hashes": s.execute(
                select(func.count().label("n")).select_from(SuppressionHash)).mappings().first()["n"],
            "inbox_pending":  s.execute(
                select(func.count().label("n")).select_from(InboxRow).where(InboxRow.materialised_at.is_(None))
            ).mappings().first()["n"],
            "outbox_pending": s.execute(
                select(func.count().label("n")).select_from(OutboxRow).where(OutboxRow.pushed_at.is_(None))
            ).mappings().first()["n"],
            "sync_cursor":    int((s.execute(
                select(SyncState.value).where(SyncState.key == "last_pulled_event_id")
            ).mappings().first() or {"value": "0"})["value"]),
        }
    return {"status": "ok", "role": "terminal", "actor": TERMINAL_ACTOR, **counts}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=brand.ICON_PNG, media_type="image/png")


# Sync visibility endpoints (read-only)

@app.get("/sync/status")
async def sync_status():
    """Local view: cursor, pending in/out, recent inbox events."""
    with db.session() as s:
        cursor = (s.execute(
            select(SyncState.value).where(SyncState.key == "last_pulled_event_id")
        ).mappings().first() or {"value": "0"})["value"]
        inbox_pending  = s.execute(
            select(func.count().label("n")).select_from(InboxRow).where(InboxRow.materialised_at.is_(None))
        ).mappings().first()["n"]
        outbox_pending = s.execute(
            select(func.count().label("n")).select_from(OutboxRow).where(OutboxRow.pushed_at.is_(None))
        ).mappings().first()["n"]
        recent_inbox = [dict(r) for r in s.execute(
            select(InboxRow.event_id, InboxRow.table_name, InboxRow.row_id, InboxRow.revision, InboxRow.actor,
                   InboxRow.received_at)
            .order_by(InboxRow.received_at.desc()).limit(20)
        ).mappings().all()]
    return {
        "actor":          TERMINAL_ACTOR,
        "cursor":         int(cursor),
        "inbox_pending":  inbox_pending,
        "outbox_pending": outbox_pending,
        "recent_inbox":   recent_inbox,
    }


# Launch

@app.post("/admin/campaigns/{campaign_id}/launch")
async def admin_campaign_launch(campaign_id: int, actor: str = "operator:unknown",
                                 _session=Depends(require_session)):
    """Launch - builds packs locally, encrypts with K_temp, pushes to cloud /packs."""
    try:
        return await launch.launch_campaign(campaign_id, actor=actor)
    except launch.LaunchError as e:
        raise HTTPException(400, str(e))
    except IntegrityError:
        raise HTTPException(409, launch._concurrent_press_detail("launch"))
    except SQLAlchemyError:
        log.exception("launch failed")
        raise HTTPException(500, "launch failed: a database error; the details are in the terminal log")
    except Exception as e:
        log.exception("launch failed: %s", e)
        raise HTTPException(500, f"launch failed: {e}")


@app.get("/admin/system/status")
async def system_status(_session=Depends(require_session)):
    """Returns setup health: handshake, K_temp, env, webhook health."""
    import time as _time
    from mailman.terminal import encryption, settings as _settings

    # Handshake / bearer
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    handshake_done = bearer_path.exists() and bearer_path.read_text().strip() != ""

    # K_temp
    k_temp_path = encryption.KEYS_DIR / "k_temp.bin"
    k_temp_age_s = None
    k_temp_ttl_s = _settings.get_int("default_k_temp_ttl_s", 86400)
    if k_temp_path.exists():
        k_temp_age_s = int(_time.time() - k_temp_path.stat().st_mtime)

    # Bootstrap token check
    bootstrap = os.environ.get("BOOTSTRAP_TOKEN", "")
    bootstrap_ok = bool(bootstrap) and bootstrap not in ("changeme", "your-token-here", "CHANGEME")

    # Entity + template + contact counts
    with db.session() as s:
        entity_count    = s.execute(select(func.count()).select_from(Entity)).scalar_one()
        entity_key_ct   = s.execute(
            select(func.count()).select_from(Entity).where(Entity.resend_key_enc.is_not(None))
        ).scalar_one()
        template_count  = s.execute(select(func.count()).select_from(Template)).scalar_one()
        contact_count   = s.execute(select(func.count()).select_from(Contact)).scalar_one()
        webhook_recent  = s.execute(
            select(func.count()).select_from(InboxRow)
            .where(InboxRow.table_name == "webhook_event",
                   InboxRow.received_at >= func.datetime("now", "-24 hours"))
        ).scalar_one()
        inbox_pending   = s.execute(
            select(func.count()).select_from(InboxRow)
            .where(InboxRow.materialised_at.is_(None),
                   InboxRow.table_name.in_(("webhook_event", "suppression")))
        ).scalar_one()

    unsub_secret_set   = bool(_settings.get("unsub_secret",   ""))
    erasure_secret_set = bool(_settings.get("erasure_secret", ""))

    return {
        "handshake_done":        handshake_done,
        "bootstrap_ok":          bootstrap_ok,
        "k_temp_age_s":          k_temp_age_s,
        "k_temp_ttl_s":          k_temp_ttl_s,
        "k_temp_expiring":       (k_temp_age_s is not None and k_temp_age_s > k_temp_ttl_s * 0.8),
        "entity_count":          entity_count,
        "entity_with_key":       entity_key_ct,
        "template_count":        template_count,
        "contact_count":         contact_count,
        "webhook_24h":           webhook_recent,
        "inbox_pending":         inbox_pending,
        "unsub_secret_set":      unsub_secret_set,
        "erasure_secret_set":    erasure_secret_set,
        "using_dev_hmac":        not unsub_secret_set or not erasure_secret_set,
    }


@app.get("/admin/audit-log")
async def admin_audit_log(limit: int = 200, _session=Depends(require_session)):
    with db.session() as s:
        rows = [dict(r) for r in s.execute(
            select(*cols(AuditEvent)).order_by(AuditEvent.occurred_at.desc()).limit(limit)
        ).mappings().all()]
    return {"events": rows}
