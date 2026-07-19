#!/usr/bin/env python3
"""bin/_backup.py - full-stack backup (§9 + §13.8).

Covers ALL persistent state across the split architecture:

  Terminal (local)
    state.sqlite.enc     - contacts, campaigns, entities, operators, settings,
                           suppression list, inbox/outbox, campaign_stages
    keys.tar.enc         - cloud bearer, K_temp, KEK, all keypairs

  Cloud (AWS)
    dynamo_snapshot.enc  - full DynamoDB table scan (packs, events, settings,
                           bearers, webhook event log, K_temp entries)
    sqs_state.json       - in-flight SQS message counts (plain, not sensitive)

  Verification
    manifest.json        - architecture map, secret-sync check, unmaterialised
                           inbox count, gap warnings, restore instructions

Encryption: AES-256-GCM, key derived from passphrase via scrypt
(N=2**14, r=8, p=1). Salt + per-file nonces stored in manifest.

NOTE ON CLOUD DB: this stack uses DynamoDB (AWS), not Postgres/Neon/MySQL.
The dynamo_snapshot covers the entire single-table design. No other cloud
database exists in this architecture.

Restore: bin/v3 restore <zip> - decrypts with passphrase, re-imports
DynamoDB items, restores SQLite + keys to volumes.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


KEYS_DIR   = Path(os.environ.get("TERMINAL_KEYS_DIR", "/var/lib/terminal/keys"))
DB_PATH    = Path(os.environ.get("VAULT_DB_PATH",    "/var/lib/terminal/state.sqlite"))
ACTOR      = os.environ.get("TERMINAL_ACTOR", "operator")
BACKUP_OUT = Path(os.environ.get("BACKUP_OUT_DIR",  "/work/backups"))


# crypto helpers

def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    return kdf.derive(passphrase.encode())


def encrypt(plaintext: bytes, key: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data=None)
    return nonce, ct


def decrypt(ciphertext: bytes, nonce: bytes, key: bytes) -> bytes:
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data=None)


def _enc_entry(nonce: bytes, plain_bytes: bytes) -> dict:
    """Store nonce + SHA-256 checksum of plaintext for integrity verification."""
    return {
        "nonce_b64":   base64.b64encode(nonce).decode(),
        "plain_size":  len(plain_bytes),
        "sha256":      hashlib.sha256(plain_bytes).hexdigest(),
    }


def _hmac_manifest(manifest_bytes: bytes, key: bytes) -> str:
    """HMAC-SHA256 of the manifest content - detects tampering."""
    import hmac as _hmac
    return _hmac.new(key, manifest_bytes, hashlib.sha256).hexdigest()


def _verify_zip(path: Path, key: bytes, manifest: dict) -> list[str]:
    """Post-write integrity check - decrypt each file and verify SHA-256."""
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                errors.append("manifest.json missing from zip")
                return errors
            for fname, entry in manifest.get("files", {}).items():
                if entry is None:
                    continue
                if fname not in names:
                    errors.append(f"{fname} missing from zip")
                    continue
                ct = zf.read(fname)
                nonce = base64.b64decode(entry["nonce_b64"])
                try:
                    plain = decrypt(ct, nonce, key)
                except Exception as e:
                    errors.append(f"{fname}: decryption failed: {e}")
                    continue
                actual_sha = hashlib.sha256(plain).hexdigest()
                if actual_sha != entry.get("sha256"):
                    errors.append(f"{fname}: SHA-256 mismatch (corrupt)")
    except Exception as e:
        errors.append(f"zip open failed: {e}")
    return errors


# gap 4: force materialisation check

def check_inbox(db_path: Path) -> dict:
    """Count unmaterialised inbox rows - backup still proceeds but warns."""
    if not db_path.exists():
        return {"error": "DB not found"}
    try:
        conn = sqlite3.connect(db_path)
        total   = conn.execute("SELECT count(*) FROM inbox").fetchone()[0]
        # 'pack' rows are handled by outcomes_loop, not the materialiser - exclude from warning
        pending = conn.execute(
            "SELECT count(*) FROM inbox WHERE materialised_at IS NULL "
            "AND table_name IN ('webhook_event', 'suppression')"
        ).fetchone()[0]
        pack_pending = conn.execute(
            "SELECT count(*) FROM inbox WHERE materialised_at IS NULL AND table_name = 'pack'"
        ).fetchone()[0]
        conn.close()
        return {"total_inbox": total, "unmaterialised": pending,
                "pack_pending_outcomes_loop": pack_pending,
                "warning": (f"{pending} webhook/suppression inbox rows not yet materialised - "
                            "wait for materialiser loop before backup for full fidelity") if pending else None}
    except Exception as e:
        return {"error": str(e)}


# gap 2: secret sync check

def check_secret_sync(db_path: Path) -> dict:
    """Hash terminal-side unsub/erasure secrets so you can verify cloud parity post-restore."""
    result: dict = {}
    if not db_path.exists():
        return {"error": "DB not found"}
    try:
        conn = sqlite3.connect(db_path)
        for skey in ("unsub_secret", "erasure_secret"):
            row = conn.execute("SELECT value FROM settings WHERE key=?", (skey,)).fetchone()
            val = row[0] if row and row[0] else None
            if val:
                # Hash the stored value (may be encrypted blob) - 16 hex chars to verify parity, not brute-forceable
                result[f"{skey}_sha256_prefix"] = hashlib.sha256(val.encode()).hexdigest()[:16]
            else:
                result[f"{skey}_sha256_prefix"] = None
                result[f"{skey}_warning"] = f"{skey} not configured in terminal settings - unsubscribe links will not work"
        conn.close()
        result["note"] = ("After restore: verify DynamoDB SETTING#unsub_secret and SETTING#erasure_secret "
                          "match terminal settings. Mismatch breaks all existing unsubscribe/erasure links.")
    except Exception as e:
        result["error"] = str(e)
    return result


# gap 1: DynamoDB full scan

def snapshot_dynamo() -> tuple[bytes, dict]:
    """Full DynamoDB table scan -> JSON bytes + stats dict."""
    try:
        import boto3
        from boto3.dynamodb.types import TypeDeserializer
    except ImportError:
        return b"", {"error": "boto3 not available in backup environment"}

    table_name = os.environ.get("DYNAMODB_TABLE", "")
    if not table_name:
        return b"", {"error": "DYNAMODB_TABLE env var not set"}

    kwargs: dict = {"region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1")}
    endpoint = os.environ.get("DYNAMODB_ENDPOINT") or os.environ.get("DYNAMODB_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint

    def _safe(v):
        """Recursively make DynamoDB values JSON-safe."""
        import decimal
        from boto3.dynamodb.types import Binary
        if isinstance(v, (bytes, Binary)):
            return {"__bytes_b64": base64.b64encode(bytes(v)).decode()}
        if isinstance(v, decimal.Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
        if isinstance(v, set):
            return [_safe(i) for i in v]
        if isinstance(v, dict):
            return {k2: _safe(v2) for k2, v2 in v.items()}
        if isinstance(v, list):
            return [_safe(i) for i in v]
        return v

    try:
        client = boto3.client("dynamodb", **kwargs)
        deser  = TypeDeserializer()
        items: list = []
        paginator = client.get_paginator("scan")
        for page in paginator.paginate(TableName=table_name):
            for raw in page["Items"]:
                items.append({k: _safe(deser.deserialize(v)) for k, v in raw.items()})

        # Tally by record type (pk prefix)
        counts: dict[str, int] = {}
        for item in items:
            prefix = str(item.get("pk", "?")).split("#")[0]
            counts[prefix] = counts.get(prefix, 0) + 1

        payload = json.dumps(items).encode()
        stats = {
            "table":       table_name,
            "item_count":  len(items),
            "by_type":     counts,
            "endpoint":    endpoint or "real AWS",
        }
        return payload, stats
    except Exception as e:
        return b"", {"error": str(e)}


# gap 3: SQS in-flight count

def snapshot_sqs() -> dict:
    """Read approximate SQS message counts (in-flight packs not yet dispatched)."""
    try:
        import boto3
    except ImportError:
        return {"error": "boto3 not available"}

    queue_url = os.environ.get("SQS_QUEUE_URL", "")
    if not queue_url:
        return {"error": "SQS_QUEUE_URL not set"}

    kwargs: dict = {"region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1")}
    sqs_endpoint = os.environ.get("SQS_ENDPOINT") or os.environ.get("LOCALSTACK_URL")
    if sqs_endpoint:
        kwargs["endpoint_url"] = sqs_endpoint

    try:
        client = boto3.client("sqs", **kwargs)
        attrs  = client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        visible   = int(attrs.get("ApproximateNumberOfMessages", 0))
        in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
        return {
            "queue_url":     queue_url,
            "visible":       visible,
            "in_flight":     in_flight,
            "warning":       (f"{visible + in_flight} packs still in SQS - "
                              "dispatcher may not have finished. Packs are recoverable by "
                              "re-launching the campaign after restore.") if (visible + in_flight) else None,
        }
    except Exception as e:
        return {"error": str(e)}


# gap 1 (continued): terminal snapshots

def snapshot_db() -> bytes:
    if not DB_PATH.exists():
        return b""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def snapshot_keys() -> bytes:
    if not KEYS_DIR.exists():
        return b""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(KEYS_DIR.iterdir()):
            if p.is_file():
                tar.add(p, arcname=p.name)
    return buf.getvalue()


# gap 5: architecture manifest

ARCHITECTURE = {
    "cloud_database":  "AWS DynamoDB (single-table design) - NOT Postgres/Neon/MySQL",
    "cloud_queue":     "AWS SQS (email pack dispatch queue)",
    "cloud_compute":   "AWS Lambda (ep-api + ep-dispatcher) or local dispatcher",
    "terminal_db":     "SQLite at /var/lib/terminal/state.sqlite",
    "terminal_keys":   "File-based at /var/lib/terminal/keys/",
    "email_provider":  "Resend (API key stored per-entity, encrypted in SQLite)",
    "restore_order": [
        "1. Decrypt zip with passphrase",
        "2. Restore SQLite to terminal volume",
        "3. Restore keys/ to terminal volume",
        "4. Re-import DynamoDB items from dynamo_snapshot.json (bin/v3 restore-dynamo)",
        "5. Verify unsub_secret + erasure_secret match between terminal settings and DynamoDB SETTING#* items",
        "6. Restart terminal - sync will resume from last cursor",
        "7. Re-queue any SQS messages if in-flight count was > 0 (re-launch affected campaigns)",
    ],
}


# main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--passphrase-env", default=None,
                    help="Read passphrase from this env var (non-interactive)")
    ap.add_argument("--skip-dynamo", action="store_true",
                    help="Skip DynamoDB scan (faster, less complete)")
    args = ap.parse_args()

    if args.passphrase_env:
        pw = os.environ.get(args.passphrase_env, "")
        if not pw:
            sys.exit(f"env var {args.passphrase_env} empty")
    else:
        pw = getpass.getpass("Backup passphrase: ")
        pw2 = getpass.getpass("Confirm: ")
        if pw != pw2:
            sys.exit("passphrases don't match")
        if len(pw) < 12:
            sys.exit("passphrase must be ≥ 12 chars")

    BACKUP_OUT.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = BACKUP_OUT / f"v3-backup-{ACTOR}-{ts}.zip"
    salt     = os.urandom(16)
    key      = derive_key(pw, salt)

    # gap 4: inbox pre-check
    print("[backup] checking unmaterialised inbox events...")
    inbox_info = check_inbox(DB_PATH)
    if inbox_info.get("warning"):
        print(f"[backup] WARN: {inbox_info['warning']}")

    # gap 2: secret sync check
    print("[backup] checking secret sync state...")
    secret_sync = check_secret_sync(DB_PATH)

    # terminal SQLite
    print(f"[backup] snapshotting SQLite from {DB_PATH}...")
    db_bytes = snapshot_db()
    print(f"[backup] SQLite: {len(db_bytes):,} bytes")

    # terminal keys
    print(f"[backup] snapshotting keys from {KEYS_DIR}...")
    keys_bytes = snapshot_keys()
    key_files  = sorted(p.name for p in KEYS_DIR.iterdir() if p.is_file()) if KEYS_DIR.exists() else []
    print(f"[backup] keys: {len(keys_bytes):,} bytes ({len(key_files)} files: {', '.join(key_files)})")

    # gap 1: DynamoDB
    dynamo_bytes: bytes = b""
    dynamo_stats: dict  = {}
    if not args.skip_dynamo:
        print("[backup] scanning DynamoDB (this may take a moment)...")
        dynamo_bytes, dynamo_stats = snapshot_dynamo()
        if dynamo_stats.get("error"):
            print(f"[backup] WARN: DynamoDB scan failed: {dynamo_stats['error']}")
        else:
            print(f"[backup] DynamoDB: {len(dynamo_bytes):,} bytes "
                  f"({dynamo_stats.get('item_count', 0)} items across {dynamo_stats.get('by_type', {})})")
    else:
        dynamo_stats = {"skipped": True}
        print("[backup] DynamoDB scan skipped (--skip-dynamo)")

    # gap 3: SQS
    print("[backup] checking SQS queue state...")
    sqs_info = snapshot_sqs()
    if sqs_info.get("warning"):
        print(f"[backup] WARN: {sqs_info['warning']}")
    elif sqs_info.get("error"):
        print(f"[backup] WARN: SQS check failed: {sqs_info['error']}")
    else:
        print(f"[backup] SQS: {sqs_info.get('visible', 0)} visible, "
              f"{sqs_info.get('in_flight', 0)} in-flight")

    # encrypt
    db_nonce,     db_ct     = encrypt(db_bytes,     key) if db_bytes     else (b"", b"")
    keys_nonce,   keys_ct   = encrypt(keys_bytes,   key) if keys_bytes   else (b"", b"")
    dynamo_nonce, dynamo_ct = encrypt(dynamo_bytes, key) if dynamo_bytes else (b"", b"")

    # manifest (with per-file SHA-256 for integrity)
    manifest = {
        "version":      3,
        "actor":        ACTOR,
        "created_at":   ts,
        "architecture": ARCHITECTURE,
        "kdf":          {"algo": "scrypt", "n": 2**14, "r": 8, "p": 1, "length": 32,
                         "salt_b64": base64.b64encode(salt).decode()},
        "cipher":       "AES-256-GCM",
        "files": {
            "state.sqlite.enc":    _enc_entry(db_nonce,     db_bytes)     if db_bytes     else None,
            "keys.tar.enc":        _enc_entry(keys_nonce,   keys_bytes)   if keys_bytes   else None,
            "dynamo_snapshot.enc": _enc_entry(dynamo_nonce, dynamo_bytes) if dynamo_bytes else None,
        },
        "key_files":    key_files,
        "dynamo":       dynamo_stats,
        "sqs":          sqs_info,
        "secret_sync":  secret_sync,
        "inbox":        inbox_info,
        "gaps_covered": {
            "terminal_sqlite":           bool(db_bytes),
            "terminal_keys":             bool(keys_bytes),
            "dynamo_full_scan":          bool(dynamo_bytes),
            "sqs_state_recorded":        "error" not in sqs_info,
            "secret_sync_checked":       "error" not in secret_sync,
            "inbox_unmaterialised_warn": inbox_info.get("unmaterialised", 0) == 0,
        },
    }

    # HMAC over manifest content - detects post-write tampering
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    manifest["manifest_hmac"] = _hmac_manifest(manifest_bytes, key)
    manifest_bytes = json.dumps(manifest, indent=2).encode()  # re-encode with HMAC

    # atomic write (temp -> rename)
    tmp_path = out_path.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json",       manifest_bytes)
            if db_ct:     zf.writestr("state.sqlite.enc",    db_ct)
            if keys_ct:   zf.writestr("keys.tar.enc",        keys_ct)
            if dynamo_ct: zf.writestr("dynamo_snapshot.enc", dynamo_ct)
            zf.writestr("sqs_state.json", json.dumps(sqs_info, indent=2))
        tmp_path.rename(out_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        sys.exit(f"[backup] FATAL: write failed: {e}")

    # harden permissions (owner-read-only)
    out_path.chmod(0o600)

    # post-write integrity verification
    print("[backup] verifying integrity of written backup...")
    verify_errors = _verify_zip(out_path, key, manifest)
    if verify_errors:
        print(f"[backup] INTEGRITY FAILURE - backup may be corrupt:")
        for err in verify_errors:
            print(f"  ✗ {err}")
        sys.exit(1)
    print("[backup] integrity check passed (decrypt + SHA-256 verified for all files)")

    size = out_path.stat().st_size
    print()
    covered = manifest["gaps_covered"]
    all_ok  = all(covered.values())
    status  = "✓ COMPLETE" if all_ok else "⚠ PARTIAL"
    print(f"[backup] {status} - wrote {out_path} ({size:,} bytes, chmod 600)")
    print()
    print("Coverage:")
    for gap, ok in covered.items():
        print(f"  {'✓' if ok else '✗'} {gap.replace('_', ' ')}")
    warnings = [inbox_info.get("warning"), sqs_info.get("warning"),
                *[v for k, v in secret_sync.items() if k.endswith("_warning")]]
    for w in warnings:
        if w: print(f"\n  ⚠ {w}")
    print()
    print("REMINDER (§9 - 6-location backup target):")
    print("  1. This zip is location 1 (operator local)")
    print("  2. Copy to: USB drive, encrypted external HD, cloud storage, remote host")
    print("  3. Restore passphrase: store separately from the zip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
