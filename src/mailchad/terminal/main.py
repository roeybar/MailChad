"""
terminal - v3.8 local admin UI + sync client.

Runs on each party's laptop. Localhost binding only (:8000 -> 127.0.0.1).

Spec sections wired:
  §3.3   sync_client.pull_loop + push_loop + outcomes_loop background tasks
  §6     bearer-authed cloud client
  §13.4  sync protocol implementation
  §13.5  pack lifecycle - launch.py builds/encrypts/pushes packs to /packs
  §13.7  init-handshake - bin/v3 init-handshake script
  §13.8  one-click backup - bin/v3 backup verb
"""
from __future__ import annotations

from mailchad import __version__

import asyncio
import logging
import os

from fastapi import Depends, FastAPI, HTTPException

from mailchad.terminal import db, routes_admin, auth, admin_ui, sync_client, launch, settings, settings_api, admin_settings_ui
from mailchad.terminal.auth import require_session


def _migrate_admin_to_operators() -> None:
    """One-time migration: copy settings admin creds into operators table if table is empty."""
    try:
        with db.conn() as c:
            count = c.execute("SELECT count(*) AS n FROM operators").fetchone()
            if count and count["n"] > 0:
                return
            admin_email = settings.get("admin_email", "") or ""
            admin_hash  = settings.get("admin_password_hash", "") or ""
            if admin_email and admin_hash:
                c.execute(
                    "INSERT OR IGNORE INTO operators (email, password_hash, role) VALUES (?, ?, 'admin')",
                    (admin_email.strip().lower(), admin_hash),
                )
                c.commit()
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

    # Start sync background tasks (per §13.4).
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
    with db.conn() as c:
        counts = {
            "contacts":  c.execute("SELECT count(*) AS n FROM contacts").fetchone()["n"],
            "campaigns": c.execute("SELECT count(*) AS n FROM campaigns").fetchone()["n"],
            "suppression_hashes": c.execute("SELECT count(*) AS n FROM suppression_hashes").fetchone()["n"],
            "inbox_pending":  c.execute("SELECT count(*) AS n FROM inbox WHERE materialised_at IS NULL").fetchone()["n"],
            "outbox_pending": c.execute("SELECT count(*) AS n FROM outbox WHERE pushed_at IS NULL").fetchone()["n"],
            "sync_cursor":    int((c.execute("SELECT value FROM sync_state WHERE key='last_pulled_event_id'").fetchone() or {"value": "0"})["value"]),
        }
    return {"status": "ok", "role": "terminal", "actor": TERMINAL_ACTOR, **counts}


# Sync visibility endpoints (read-only)

@app.get("/sync/status")
async def sync_status():
    """Local view: cursor, pending in/out, recent inbox events."""
    with db.conn() as c:
        cursor = (c.execute("SELECT value FROM sync_state WHERE key='last_pulled_event_id'").fetchone() or {"value": "0"})["value"]
        inbox_pending  = c.execute("SELECT count(*) AS n FROM inbox WHERE materialised_at IS NULL").fetchone()["n"]
        outbox_pending = c.execute("SELECT count(*) AS n FROM outbox WHERE pushed_at IS NULL").fetchone()["n"]
        recent_inbox = [dict(r) for r in c.execute(
            "SELECT event_id, table_name, row_id, revision, actor, received_at "
            "FROM inbox ORDER BY received_at DESC LIMIT 20"
        ).fetchall()]
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
    except Exception as e:
        log.exception("launch failed: %s", e)
        raise HTTPException(500, f"launch failed: {e}")


@app.get("/admin/system/status")
async def system_status(_session=Depends(require_session)):
    """Returns setup health: handshake, K_temp, env, webhook health."""
    import time as _time
    from mailchad.terminal import encryption, settings as _settings

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
    with db.conn() as c:
        entity_count    = c.execute("SELECT count(*) FROM entities").fetchone()[0]
        entity_key_ct   = c.execute("SELECT count(*) FROM entities WHERE resend_key_enc IS NOT NULL").fetchone()[0]
        template_count  = c.execute("SELECT count(*) FROM templates").fetchone()[0]
        contact_count   = c.execute("SELECT count(*) FROM contacts").fetchone()[0]
        webhook_recent  = c.execute(
            "SELECT count(*) FROM inbox WHERE table_name='webhook_event' "
            "AND received_at >= datetime('now','-24 hours')"
        ).fetchone()[0]
        inbox_pending   = c.execute(
            "SELECT count(*) FROM inbox WHERE materialised_at IS NULL "
            "AND table_name IN ('webhook_event','suppression')"
        ).fetchone()[0]

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
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM audit_event ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()]
    return {"events": rows}
