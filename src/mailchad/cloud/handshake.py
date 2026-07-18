"""Init handshake endpoint - cloud side (spec §7 + §13.7).

ONE-TIME setup. Each party POSTs their KEM pubkey + a bootstrap token;
cloud validates, registers the pubkey in the `pubkey` table, issues a
bearer token (stored as SHA-256 in `terminal_session`).

The bootstrap token is a long random string set via the BOOTSTRAP_TOKEN
env var on the cloud. It allows up to 2 registrations (operator + client),
one each. After both roles are registered, additional handshake attempts
with the same token are rejected - operator must rotate the token to
re-handshake (e.g. after revoking compromised credentials).

Bearer tokens issued at handshake are long-lived but rotatable via
DELETE /init/session (revokes a single bearer) or by re-running handshake
with a fresh bootstrap token (revokes all + re-issues).
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from mailchad.cloud import dynamo
from mailchad.cloud.rate_limit import limiter

log = logging.getLogger("cloud.handshake")
router = APIRouter()


def _bootstrap_token() -> str:
    return os.environ.get("BOOTSTRAP_TOKEN", "")


def _verify_bootstrap(supplied: str) -> bool:
    expected = _bootstrap_token()
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected, supplied)


class HandshakeIn(BaseModel):
    role:    str      # "operator" | "client"
    kem_pub: str      # base64 of 32-byte X25519 pubkey

HandshakeIn.model_rebuild()


@router.post("/init/handshake")
@limiter.limit("5/minute")
async def init_handshake(
    request: Request,
    body: HandshakeIn,
    x_bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
):
    if not _verify_bootstrap(x_bootstrap_token or ""):
        raise HTTPException(401, "bad or missing X-Bootstrap-Token")

    if body.role not in ("operator", "client"):
        raise HTTPException(400, "role must be 'operator' or 'client'")

    try:
        kem_pub_bytes = base64.b64decode(body.kem_pub)
    except Exception as e:
        raise HTTPException(400, f"bad base64 kem_pub: {e}")
    if len(kem_pub_bytes) != 32:
        raise HTTPException(400, f"kem_pub must decode to 32 bytes, got {len(kem_pub_bytes)}")

    # Refuse re-registration of the same role unless the operator first
    # explicitly wiped via DELETE /init/handshake/{role}.
    existing = dynamo.get_pubkey(body.role)
    if existing is not None:
        raise HTTPException(
            409,
            f"role {body.role!r} already registered; "
            f"DELETE /init/handshake/{body.role} to revoke first",
        )

    # Register pubkey
    dynamo.put_pubkey(body.role, kem_pub_bytes)

    # Issue bearer
    bearer = secrets.token_urlsafe(48)
    bearer_hash = hashlib.sha256(bearer.encode()).hexdigest()
    dynamo.put_session(bearer_hash, body.role)

    log.info("handshake: registered %s pubkey + issued bearer", body.role)
    return {
        "registered": body.role,
        "bearer":     bearer,                       # show ONCE; terminal stores it
        "fingerprint": hashlib.sha256(kem_pub_bytes).hexdigest()[:16],
    }


@router.get("/init/handshake/status")
async def handshake_status():
    """Read-only - which roles have registered. Safe to call publicly."""
    rows = dynamo.list_pubkeys()
    return {
        "registered_roles": rows,
        "bootstrap_token_configured": bool(_bootstrap_token()),
    }


@router.delete("/init/handshake/{role}")
async def handshake_revoke(
    role: str,
    x_bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
):
    """Wipe a role's pubkey + all its bearer tokens. Requires bootstrap token."""
    if not _verify_bootstrap(x_bootstrap_token or ""):
        raise HTTPException(401, "bad or missing X-Bootstrap-Token")
    if role not in ("operator", "client"):
        raise HTTPException(400, "bad role")
    dynamo.delete_pubkey(role)
    dynamo.revoke_sessions_for_actor(role)
    return {"revoked": role}


@router.get("/pubkeys")
async def get_pubkeys():
    """Public - returns both registered pubkeys (base64). Anyone can read; they're public.
    Used by terminals + cloud's webhook receiver (§4.3) for encrypt-to-both."""
    pubkeys = dynamo.get_all_pubkeys()
    return {
        actor: base64.b64encode(kem_pub_bytes).decode("ascii")
        for actor, kem_pub_bytes in pubkeys.items()
    }
