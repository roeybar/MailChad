"""HMAC-signed tokens for public unsubscribe + GDPR erasure links.

Ported from coherence/backend/auth/app/compliance_tokens.py.

Two distinct secrets - UNSUB_SECRET and ERASURE_SECRET - so a leaked
unsubscribe link can't be repurposed for mass deletion.

Token format:  <payload_b64u>.<sig_b64u>
where payload = json({"h": email_hash, "n": nonce, "x": expiry_unix})
and   sig     = HMAC-SHA256(secret, payload_b64u)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Literal

from mailchad.cloud import settings

TokenKind = Literal["unsub", "erasure"]


def _unsub_ttl() -> int:
    return settings.get_int("unsub_token_ttl_s",
                             int(os.environ.get("UNSUB_TOKEN_TTL_S", str(60 * 60 * 24 * 365))))


def _erasure_ttl() -> int:
    return settings.get_int("erasure_token_ttl_s",
                             int(os.environ.get("ERASURE_TOKEN_TTL_S", str(60 * 60 * 24 * 30))))


class TokenError(ValueError):
    pass


class TokenExpired(TokenError):
    pass


class TokenInvalid(TokenError):
    pass


def _secret_for(kind: TokenKind) -> bytes:
    if kind == "unsub":
        s = settings.get("unsub_secret", "") or ""
    elif kind == "erasure":
        s = settings.get("erasure_secret", "") or ""
    else:
        raise TokenError(f"unknown kind {kind!r}")
    if not s:
        # Dev-only fallback; loudly logged. Prod MUST set the env.
        return hashlib.sha256(f"dev-only-{kind}-secret".encode("utf-8")).digest()
    return s.encode("utf-8")


def _b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint(kind: TokenKind, *, email_hash: str, ttl_s: int | None = None) -> str:
    if ttl_s is None:
        ttl_s = _unsub_ttl() if kind == "unsub" else _erasure_ttl()
    payload = {"h": email_hash, "n": secrets.token_urlsafe(8), "x": int(time.time()) + ttl_s}
    payload_b = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64u_encode(payload_b)
    sig = hmac.new(_secret_for(kind), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u_encode(sig)}"


def verify(kind: TokenKind, token: str) -> str:
    """Returns email_hash on success. Raises TokenExpired or TokenInvalid."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        raise TokenInvalid("malformed token")
    expected = hmac.new(_secret_for(kind), payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u_decode(sig_b64), expected):
        raise TokenInvalid("bad signature")
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception as e:
        raise TokenInvalid(f"payload not JSON: {e}") from e
    if not isinstance(payload, dict) or "h" not in payload or "x" not in payload:
        raise TokenInvalid("payload shape")
    if int(payload["x"]) < int(time.time()):
        raise TokenExpired("expired")
    return str(payload["h"])
