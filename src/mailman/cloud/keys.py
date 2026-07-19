"""K_temp lifecycle - cloud side (§2.3, §13 step 3).

Cloud is the time-bounded oracle. It holds K_temp for TTL seconds, can decrypt
packs during that window via terminal/app/encryption.py:decrypt_with_temp, and
wipes K_temp on TTL expiry.

K_temp is symmetric (32 bytes). Terminals mint it (see terminal/app/encryption.py:
mint_k_temp), then ship it to the cloud via POST /key/temp. Cloud stores it on
disk with 0600 perms inside its data volume.

The cloud NEVER mints K_temp itself. If no terminal has provisioned one and
no pending packs need decrypting, the cloud is in a perfectly secure state:
nothing to leak.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

KEYS_DIR = Path(os.environ.get("CLOUD_KEYS_DIR", "/var/lib/cloud/keys"))
K_TEMP_PATH = KEYS_DIR / "k_temp.bin"
K_TEMP_META = KEYS_DIR / "k_temp.meta.json"

VALID_TTLS = (3600, 86400, 604800)  # 1h, 24h, 7d


class TTLViolation(ValueError):
    pass


def get_active_k_temp() -> bytes | None:
    """Returns K_temp bytes if present AND not expired. Otherwise None.

    A None return means the dispatcher must refuse to decrypt anything;
    terminals can refresh via POST /key/temp.
    """
    if not K_TEMP_PATH.exists() or not K_TEMP_META.exists():
        return None
    meta = json.loads(K_TEMP_META.read_text())
    if int(time.time()) > meta["expires_at"]:
        # TTL expired - wipe + return None.
        wipe_k_temp()
        return None
    return K_TEMP_PATH.read_bytes()


def set_k_temp(k_temp: bytes, ttl_seconds: int, set_by: str) -> dict:
    """Replace the current K_temp with a fresh one. Returns metadata.

    set_by: 'operator' or 'client' - for audit (which terminal provisioned it).
    """
    if len(k_temp) != 32:
        raise ValueError(f"K_temp must be 32 bytes, got {len(k_temp)}")
    if ttl_seconds not in VALID_TTLS:
        raise TTLViolation(f"ttl_seconds must be one of {VALID_TTLS}, got {ttl_seconds}")
    if set_by not in ("operator", "client"):
        raise ValueError(f"set_by must be 'operator' or 'client', got {set_by!r}")

    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    K_TEMP_PATH.write_bytes(k_temp)
    os.chmod(K_TEMP_PATH, 0o600)

    now = int(time.time())
    meta = {
        "expires_at":  now + ttl_seconds,
        "set_at":      now,
        "ttl_seconds": ttl_seconds,
        "set_by":      set_by,
        "key_id":      _key_id(k_temp),
    }
    K_TEMP_META.write_text(json.dumps(meta))
    os.chmod(K_TEMP_META, 0o600)
    return meta


def wipe_k_temp() -> None:
    """Overwrite + unlink K_temp. Best-effort secure delete (on COW filesystems
    the old bytes may persist; ZFS/btrfs operators should disable snapshots on
    KEYS_DIR for prod). Idempotent."""
    if K_TEMP_PATH.exists():
        try:
            size = K_TEMP_PATH.stat().st_size
            with open(K_TEMP_PATH, "r+b") as f:
                f.write(b"\x00" * size)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        try:
            K_TEMP_PATH.unlink()
        except FileNotFoundError:
            pass
    if K_TEMP_META.exists():
        try:
            K_TEMP_META.unlink()
        except FileNotFoundError:
            pass


def k_temp_status() -> dict:
    """Health-check shape - never returns the key itself, only metadata."""
    if not K_TEMP_META.exists():
        return {"present": False}
    meta = json.loads(K_TEMP_META.read_text())
    now = int(time.time())
    return {
        "present":     True,
        "key_id":      meta.get("key_id"),
        "set_by":      meta.get("set_by"),
        "set_at":      meta.get("set_at"),
        "expires_at":  meta.get("expires_at"),
        "remaining_s": max(0, meta["expires_at"] - now),
        "ttl_seconds": meta.get("ttl_seconds"),
    }


def _key_id(k: bytes) -> str:
    return hashlib.sha256(k).hexdigest()[:8]
