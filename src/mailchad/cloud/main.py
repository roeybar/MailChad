"""
cloud - v3.2 always-on public-facing server.

Responsibilities:
- Sync API (GET /sync/pull, POST /sync/push) - mailchad/cloud/sync.py
- K_temp lifecycle endpoints
- Pack push endpoint - stores in DynamoDB + enqueues to SQS
- Public surface: /webhooks/resend, /u/<token>, /e/<token>
- Near-conflict detection at push time

Dispatch runs in a SEPARATE process (ep-dispatcher Lambda or local poller).
This server never touches plaintext - dispatcher_lambda.py owns that window.
"""

import logging
import os

from mailchad import __version__

from fastapi import FastAPI, Request
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from mailchad.cloud import dynamo, sync, handshake, packs, webhook_receiver, compliance_public, settings, settings_api
from mailchad.cloud.rate_limit import limiter

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("cloud")

app = FastAPI(title="ep-v3-cloud", version=__version__)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(sync.router)
app.include_router(handshake.router)
app.include_router(packs.router)
app.include_router(webhook_receiver.router)
app.include_router(compliance_public.router)
app.include_router(settings_api.router)


@app.on_event("startup")
async def on_startup() -> None:
    dynamo.init()
    migrated = settings.migrate_from_env()
    if migrated:
        log.info("settings: migrated %d env vars to DB: %s",
                 len(migrated), ", ".join(migrated.keys()))
    log.info("cloud ready (dispatcher runs as separate ep-dispatcher process)")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    pass


@app.get("/healthz")
async def healthz():
    from mailchad.cloud import keys_dynamo
    events = dynamo.query_events_since(0, limit=1)
    # count via GSI not available cheaply; use list calls for healthz approximations
    pending = dynamo.list_pending_packs(limit=500)
    conflicts = dynamo.list_near_conflicts(unacked_only=True)
    return {
        "status":               "ok",
        "role":                 "cloud",
        "packs_pending":        len(pending),
        "near_conflicts_unack": len(conflicts),
        "k_temp":               keys_dynamo.k_temp_status(),
    }
