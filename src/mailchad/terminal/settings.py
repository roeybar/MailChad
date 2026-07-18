"""Terminal settings - key/value config with at-rest encryption for secrets.

Mirror of cloud/app/settings.py. KEK at TERMINAL_KEYS_DIR/settings_kek.bin.

Secrets are decrypted on each .get() call - never cached as plaintext.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mailchad.terminal import db

log = logging.getLogger("terminal.settings")

KEYS_DIR = Path(os.environ.get("TERMINAL_KEYS_DIR", "/var/lib/terminal/keys"))
KEK_PATH = KEYS_DIR / "settings_kek.bin"

# Keys that MUST be encrypted at rest.
SECRET_KEYS: set[str] = {
    "admin_password_hash",   # already bcrypt'd but double-layer doesn't hurt
    "jwt_secret",
    "unsub_secret",
    "erasure_secret",
    "resend_api_key",        # transitional - eventually per-domain via domain table
}

ENV_MIGRATE: list[tuple[str, str]] = [
    # auth
    ("admin_email",            "ADMIN_EMAIL"),
    ("admin_password_hash",    "ADMIN_PASSWORD_HASH"),
    ("jwt_secret",             "JWT_SECRET"),
    ("session_ttl_s",          "SESSION_TTL_S"),

    # brand + sender
    ("email_from",             "EMAIL_FROM"),
    ("email_footer_address",   "EMAIL_FOOTER_ADDRESS"),
    ("entity_name",           "COMPANY_NAME"),
    ("support_email",          "SUPPORT_EMAIL"),
    ("public_host",            "PUBLIC_HOST"),

    # shared secrets (MUST match cloud)
    ("unsub_secret",           "UNSUB_SECRET"),
    ("erasure_secret",         "ERASURE_SECRET"),

    # tunables
    ("default_k_temp_ttl_s",   "DEFAULT_K_TEMP_TTL_S"),
    ("unsub_token_ttl_s",      "UNSUB_TOKEN_TTL_S"),
    ("erasure_token_ttl_s",    "ERASURE_TOKEN_TTL_S"),

    # transitional
    ("resend_api_key",         "RESEND_API_KEY"),
]

ENV_ONLY: set[str] = {
    "VAULT_DB_PATH",
    "TERMINAL_KEYS_DIR",
    "TERMINAL_ACTOR",
    "CLOUD_URL",
    "CLOUD_BEARER",
    "LOG_LEVEL",
    "DB_ENCRYPTION_KEY",
    "DISABLE_QUEUE_WORKER",
    "SYNC_POLL_TIMEOUT_S",
    "SYNC_PUSH_INTERVAL_S",
}

_plain_cache: dict[str, str] = {}
_cache_loaded = False
_kek: bytes | None = None


def _load_kek() -> bytes:
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
    kek = _load_kek()
    nonce = os.urandom(12)
    ct = AESGCM(kek).encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return base64.b64encode(nonce + ct).decode("ascii")


def _decrypt(blob_b64: str) -> str:
    kek = _load_kek()
    blob = base64.b64decode(blob_b64)
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(kek).decrypt(nonce, ct, associated_data=None).decode("utf-8")


def _load_plain_cache() -> None:
    global _cache_loaded
    with db.conn() as c:
        rows = c.execute("SELECT key, value, is_secret FROM settings").fetchall()
    _plain_cache.clear()
    for r in rows:
        if not r["is_secret"]:
            _plain_cache[r["key"]] = r["value"]
    _cache_loaded = True


def is_secret(key: str) -> bool:
    return key in SECRET_KEYS


def get(key: str, default: str | None = None) -> str | None:
    secret = is_secret(key)
    with db.conn() as c:
        row = c.execute(
            "SELECT value, is_secret FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row:
        if secret:
            try:
                return _decrypt(row["value"])
            except Exception as e:
                log.error("settings: failed to decrypt %s: %s", key, e)
                return default
        return row["value"]
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
    secret_flag = 1 if is_secret(key) else 0
    stored = _encrypt(value) if secret_flag else value
    with db.conn() as c:
        c.execute(
            "INSERT INTO settings (key, value, is_secret, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "is_secret=excluded.is_secret, updated_at=datetime('now'), "
            "updated_by=excluded.updated_by",
            (key, stored, secret_flag, updated_by),
        )
        c.commit()
    if not secret_flag:
        _plain_cache[key] = value


def delete(key: str) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM settings WHERE key = ?", (key,))
        c.commit()
    _plain_cache.pop(key, None)


def get_all(*, redact_secrets: bool = True) -> dict[str, str]:
    if not _cache_loaded:
        _load_plain_cache()
    out = dict(_plain_cache)
    with db.conn() as c:
        rows = c.execute("SELECT key FROM settings WHERE is_secret = 1").fetchall()
    for r in rows:
        out[r["key"]] = "<encrypted>" if redact_secrets else (get(r["key"]) or "")
    return out


def migrate_from_env() -> dict[str, str]:
    if not _cache_loaded:
        _load_plain_cache()
    _load_kek()
    migrated = {}
    for key, env_name in ENV_MIGRATE:
        with db.conn() as c:
            existing = c.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
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
