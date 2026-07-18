"""Snapshot the vault DB, encrypt, ship to backup over wg2.

Called on vault shutdown (and on demand from admin UI).

Algorithm:
  1. SQLite .backup() to a temp file (consistent online snapshot).
  2. Compress (gzip).
  3. Encrypt with AES-256-GCM using DB_ENCRYPTION_KEY (base64-32).
  4. POST to backup module at BACKUP_INTERNAL_URL/snapshot.
     Body = JSON metadata header + '\\n' + raw ciphertext.

DB_ENCRYPTION_KEY MUST be set in vault env. Backup module never sees plaintext
or the key; it stores only the ciphertext + manifest entry.

Restore: operator runs `bin/v3 restore <snapshot-id>` which pulls the
ciphertext, decrypts with the same key, gunzips, writes to vault-db volume.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mailchad.terminal import db

log = logging.getLogger("vault.snapshot")

BACKUP_URL = os.environ.get("BACKUP_INTERNAL_URL", "http://172.31.0.4:8001")


def _key() -> bytes:
    raw = os.environ.get("DB_ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError("DB_ENCRYPTION_KEY not set; refusing to snapshot")
    try:
        k = base64.b64decode(raw)
    except Exception:
        k = raw.encode("utf-8")
    if len(k) not in (16, 24, 32):
        raise RuntimeError(f"DB_ENCRYPTION_KEY must be 16/24/32 raw bytes (base64-decoded); got {len(k)}")
    return k


def _dump_db() -> bytes:
    """SQLite online backup, then gzip."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        # SQLite online backup API - consistent snapshot even with writers.
        src = sqlite3.connect(db.DB_PATH)
        dst = sqlite3.connect(tmp_path)
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        return gzip.compress(tmp_path.read_bytes())
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _encrypt(plain: bytes) -> tuple[bytes, bytes]:
    """Returns (nonce, ciphertext+tag)."""
    nonce = os.urandom(12)
    cipher = AESGCM(_key()).encrypt(nonce, plain, associated_data=None)
    return nonce, cipher


async def create_and_push() -> dict:
    """Create snapshot + ship to backup. Returns summary."""
    snapshot_id = str(uuid.uuid4())[:16]
    timestamp = datetime.now(timezone.utc).isoformat()
    log.info("snapshot %s: dumping DB...", snapshot_id)
    plain = _dump_db()
    log.info("snapshot %s: %d bytes gzipped, encrypting...", snapshot_id, len(plain))
    nonce, ciphertext = _encrypt(plain)
    payload_blob = nonce + ciphertext   # 12-byte nonce prefix
    plain_sha = hashlib.sha256(plain).hexdigest()

    meta = {
        "snapshot_id": snapshot_id,
        "timestamp": timestamp,
        "encryption": "aes-256-gcm",
        "plain_sha256": plain_sha,
        "plain_size": len(plain),
        "cipher_size": len(payload_blob),
    }
    body = json.dumps(meta).encode("utf-8") + b"\n" + payload_blob

    log.info("snapshot %s: shipping %d bytes to %s...", snapshot_id, len(body), BACKUP_URL)
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{BACKUP_URL}/snapshot", content=body)
        r.raise_for_status()
        backup_ack = r.json()

    db.audit(actor="system:vault-snapshot", action="snapshot.shipped",
             target=f"snapshot:{snapshot_id}",
             details={"cipher_size": len(payload_blob), "ack": backup_ack})
    log.info("snapshot %s: shipped", snapshot_id)
    return {"snapshot_id": snapshot_id, "size": len(payload_blob), "ack": backup_ack}


async def propagate_erasure(user_hash: str, request_id: str) -> dict:
    """Send erasure-propagation command to backup over wg2."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{BACKUP_URL}/erase",
            json={"user_hash": user_hash, "request_id": request_id},
        )
        r.raise_for_status()
        ack = r.json()
    db.audit(actor="system:vault-erasure-propagation", action="erasure.propagated",
             target=f"hash:{user_hash}", details={"ack": ack})
    return ack


async def sync_suppression_mirror() -> dict:
    """Push current suppression_hashes to backup mirror."""
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT email_hash, reason, added_at FROM suppression_hashes"
        ).fetchall()]
    if not rows:
        return {"synced": 0}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{BACKUP_URL}/suppression-mirror/sync", json=rows)
        r.raise_for_status()
        ack = r.json()
    return {"synced": len(rows), "ack": ack}
