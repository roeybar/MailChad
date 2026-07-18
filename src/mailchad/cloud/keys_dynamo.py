"""K_temp lifecycle via DynamoDB - v3.2 (replaces keys.py filesystem storage).

DynamoDB TTL attribute auto-deletes expired items within ~48h. We also check
expires_at explicitly on read so there's no stale-key window from TTL lag.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from mailchad.cloud import dynamo

log = logging.getLogger("cloud.keys_dynamo")

VALID_TTLS = (3600, 86400, 604800)  # 1h, 24h, 7d


class TTLViolation(ValueError):
    pass


def _key_id(k: bytes) -> str:
    return hashlib.sha256(k).hexdigest()[:8]


def get_active_k_temp() -> bytes | None:
    """Return K_temp bytes if present and not expired, else None."""
    table = dynamo._get_table()
    # Scan for the single KTEMP item (there is at most one active at a time).
    # We store it under pk=KTEMP#{key_id}; look up via the 'active' pointer.
    resp = table.get_item(Key={"pk": "KTEMP_ACTIVE", "sk": "KTEMP_ACTIVE"})
    ptr = resp.get("Item")
    if not ptr:
        return None
    key_id = ptr.get("key_id")
    if not key_id:
        return None
    return _fetch_ktemp(key_id)


def _fetch_ktemp(key_id: str) -> bytes | None:
    table = dynamo._get_table()
    resp = table.get_item(Key={"pk": f"KTEMP#{key_id}", "sk": f"KTEMP#{key_id}"})
    item = resp.get("Item")
    if not item:
        return None
    if int(time.time()) > int(item["expires_at"]):
        # Expired - wipe and return None
        wipe_k_temp()
        return None
    raw = item["key_bytes"]
    return bytes(raw)


def set_k_temp(k_temp: bytes, ttl_seconds: int, set_by: str) -> dict:
    """Store a new K_temp. Overwrites any existing one. Returns metadata."""
    if len(k_temp) != 32:
        raise ValueError(f"K_temp must be 32 bytes, got {len(k_temp)}")
    if ttl_seconds not in VALID_TTLS:
        raise TTLViolation(f"ttl_seconds must be one of {VALID_TTLS}, got {ttl_seconds}")
    if set_by not in ("operator", "client"):
        raise ValueError(f"set_by must be 'operator' or 'client', got {set_by!r}")

    table = dynamo._get_table()
    now = int(time.time())
    expires_at = now + ttl_seconds
    kid = _key_id(k_temp)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Write the key item with DynamoDB TTL
    table.put_item(Item={
        "pk":          f"KTEMP#{kid}",
        "sk":          f"KTEMP#{kid}",
        "key_id":      kid,
        "key_bytes":   bytes(k_temp),
        "expires_at":  expires_at,
        "ttl":         expires_at,        # DynamoDB TTL attribute
        "set_at":      now,
        "set_by":      set_by,
        "ttl_seconds": ttl_seconds,
    })

    # Write/update the 'active' pointer
    table.put_item(Item={
        "pk":      "KTEMP_ACTIVE",
        "sk":      "KTEMP_ACTIVE",
        "key_id":  kid,
        "set_at":  now_iso,
    })

    log.info("keys: K_temp set (key_id=%s, ttl=%ds, by=%s)", kid, ttl_seconds, set_by)
    meta = {
        "key_id":      kid,
        "set_at":      now,
        "set_by":      set_by,
        "expires_at":  expires_at,
        "ttl_seconds": ttl_seconds,
    }
    return meta


def wipe_k_temp() -> None:
    """Delete K_temp and the active pointer. Idempotent."""
    table = dynamo._get_table()
    # Read pointer to find key_id
    resp = table.get_item(Key={"pk": "KTEMP_ACTIVE", "sk": "KTEMP_ACTIVE"})
    ptr = resp.get("Item")
    if ptr and ptr.get("key_id"):
        try:
            table.delete_item(Key={
                "pk": f"KTEMP#{ptr['key_id']}", "sk": f"KTEMP#{ptr['key_id']}",
            })
        except ClientError:
            pass
    try:
        table.delete_item(Key={"pk": "KTEMP_ACTIVE", "sk": "KTEMP_ACTIVE"})
    except ClientError:
        pass
    log.info("keys: K_temp wiped")


def k_temp_status() -> dict:
    """Metadata only - never returns key bytes."""
    table = dynamo._get_table()
    resp = table.get_item(Key={"pk": "KTEMP_ACTIVE", "sk": "KTEMP_ACTIVE"})
    ptr = resp.get("Item")
    if not ptr or not ptr.get("key_id"):
        return {"present": False}
    kid = ptr["key_id"]
    resp2 = table.get_item(Key={"pk": f"KTEMP#{kid}", "sk": f"KTEMP#{kid}"})
    item = resp2.get("Item")
    if not item:
        return {"present": False}
    now = int(time.time())
    expires_at = int(item["expires_at"])
    if now > expires_at:
        return {"present": False}
    return {
        "present":     True,
        "key_id":      item["key_id"],
        "set_by":      item.get("set_by"),
        "set_at":      item.get("set_at"),
        "expires_at":  expires_at,
        "remaining_s": max(0, expires_at - now),
        "ttl_seconds": int(item.get("ttl_seconds", 0)),
    }
