"""Cloud settings - key/value config with at-rest encryption for secrets.

Per v3.1 Q1 refactor + operator's security directive: secrets are encrypted
at rest with AES-256-GCM using a KEK stored in CLOUD_KEYS_DIR/settings_kek.bin
(0600). Secrets are decrypted on each .get() call - never cached as plaintext.

Non-secret settings (brand strings, public hostnames, TTLs) are stored
plaintext + cached in-memory for cheap reads.

KEK lifecycle:
- generated at first migrate_from_env() if KEYS_DIR/settings_kek.bin missing
- 32 bytes random
- 0600 file perms
- backed up alongside KEM keys via existing backup mechanism
- if KEK is lost: all encrypted settings unrecoverable (operator must re-paste
  via UI). Non-secret settings unaffected.

Only BOOTSTRAP_TOKEN stays env-only (chicken-egg with handshake auth).
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mailchad.cloud import dynamo

log = logging.getLogger("cloud.settings")

KEYS_DIR = Path(os.environ.get("CLOUD_KEYS_DIR", "/var/lib/cloud/keys"))
KEK_PATH = KEYS_DIR / "settings_kek.bin"

# Keys that MUST be encrypted at rest.
SECRET_KEYS: set[str] = {
    "resend_webhook_secret",
    "unsub_secret",
    "erasure_secret",
}

# Env vars that auto-migrate to settings on first boot.
# Each entry: (settings_key, env_var_name)
ENV_MIGRATE: list[tuple[str, str]] = [
    ("resend_webhook_secret", "RESEND_WEBHOOK_SECRET"),
    ("webhook_max_skew_s",    "WEBHOOK_MAX_SKEW_S"),
    ("unsub_secret",          "UNSUB_SECRET"),
    ("erasure_secret",        "ERASURE_SECRET"),
    ("company_name",          "COMPANY_NAME"),
    ("support_email",         "SUPPORT_EMAIL"),
    ("public_host",           "PUBLIC_HOST"),
]

# Env vars that STAY env-only (infra-bound / boot-time / chicken-egg).
ENV_ONLY: set[str] = {
    "BOOTSTRAP_TOKEN",
    "CLOUD_DB_PATH",
    "CLOUD_KEYS_DIR",
    "LOG_LEVEL",
    "DISABLE_DISPATCHER",
}

# Non-secret cache (cheap reads for brand strings, TTLs, etc.).
# Secrets are NEVER cached as plaintext - they're decrypted on each .get().
_plain_cache: dict[str, str] = {}
_cache_loaded = False
_kek: bytes | None = None


def _load_kek() -> bytes:
    """Load or generate the settings KEK. 32 bytes, 0600 perms."""
    global _kek
    if _kek is not None:
        return _kek
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if KEK_PATH.exists():
        _kek = KEK_PATH.read_bytes()
        if len(_kek) != 32:
            raise RuntimeError(f"KEK at {KEK_PATH} is {len(_kek)} bytes; expected 32")
    else:
        _kek = os.urandom(32)
        KEK_PATH.write_bytes(_kek)
        os.chmod(KEK_PATH, 0o600)
        log.info("settings: generated new KEK at %s (back up KEYS_DIR to preserve)", KEK_PATH)
    return _kek


def _encrypt(plaintext: str) -> str:
    """AES-256-GCM with KEK. Output: base64(12-byte nonce || ciphertext+tag)."""
    kek = _load_kek()
    nonce = os.urandom(12)
    ct = AESGCM(kek).encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return base64.b64encode(nonce + ct).decode("ascii")


def _decrypt(blob_b64: str) -> str:
    """Reverse of _encrypt. Raises on tamper or wrong KEK."""
    kek = _load_kek()
    blob = base64.b64decode(blob_b64)
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(kek).decrypt(nonce, ct, associated_data=None).decode("utf-8")


def _load_plain_cache() -> None:
    """Cache only NON-secret settings. Secrets are decrypted on each .get()."""
    global _cache_loaded
    rows = dynamo.list_settings()
    _plain_cache.clear()
    for r in rows:
        if not r["is_secret"]:
            _plain_cache[r["key"]] = r["value"]
    _cache_loaded = True


def is_secret(key: str) -> bool:
    """Whether a key should be stored encrypted."""
    return key in SECRET_KEYS


def get(key: str, default: str | None = None) -> str | None:
    """Read a setting. Non-secrets cached; secrets decrypted on every call.

    Falls back to env var if key is in ENV_MIGRATE and settings has no value
    (transitional - for fresh deploys where migrate_from_env hasn't been
    triggered yet)."""
    if not _cache_loaded:
        _load_plain_cache()

    if is_secret(key):
        # Always re-query + decrypt on use; never cache plaintext.
        row = dynamo.get_setting(key)
        if row and row["is_secret"]:
            try:
                return _decrypt(row["value"])
            except Exception as e:
                log.error("settings: failed to decrypt %s: %s (KEK rotation? backup restore?)",
                          key, e)
                return default
    elif key in _plain_cache:
        return _plain_cache[key]

    # Transitional fallback to env
    for k, env_name in ENV_MIGRATE:
        if k == key:
            return os.environ.get(env_name, default)
    return default


def get_int(key: str, default: int) -> int:
    v = get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def set(key: str, value: str, updated_by: str = "system") -> None:
    """Upsert. Encrypts at rest if key is in SECRET_KEYS."""
    secret_flag = 1 if is_secret(key) else 0
    stored = _encrypt(value) if secret_flag else value
    dynamo.put_setting(key, stored, is_secret=bool(secret_flag), updated_by=updated_by)
    if not secret_flag:
        _plain_cache[key] = value
    # else: don't cache plaintext


def delete(key: str) -> None:
    dynamo.delete_setting(key)
    _plain_cache.pop(key, None)


def get_all(*, redact_secrets: bool = True) -> dict[str, str]:
    """List all settings. Secrets shown as '<encrypted>' by default."""
    if not _cache_loaded:
        _load_plain_cache()
    out = dict(_plain_cache)
    rows = dynamo.list_settings()
    for r in rows:
        if r["is_secret"]:
            out[r["key"]] = "<encrypted>" if redact_secrets else (get(r["key"]) or "")
    return out


def migrate_from_env() -> dict[str, str]:
    """Copy env-only values into settings table on first boot. Idempotent.
    Returns {settings_key: env_var_name} for any keys that got moved.
    """
    if not _cache_loaded:
        _load_plain_cache()
    # Touch KEK so it's generated if absent (so first encrypted set works)
    _load_kek()
    migrated = {}
    for key, env_name in ENV_MIGRATE:
        # Check if already in DB (works for both secret + non-secret)
        existing = dynamo.get_setting(key)
        if existing:
            if os.environ.get(env_name):
                log.info("settings: %s already in DB; env var %s shadowed (safe to remove)",
                         key, env_name)
            continue
        env_value = os.environ.get(env_name)
        if env_value:
            set(key, env_value, updated_by="system:env-migration")
            migrated[key] = env_name
            log.info("settings: migrated %s from env %s%s",
                     key, env_name, " (encrypted at rest)" if is_secret(key) else "")
    return migrated
