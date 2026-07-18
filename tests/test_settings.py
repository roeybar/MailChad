"""Tests for the settings module (v3.1 Q1).

Verifies:
- KEK lifecycle (generate, reuse, secure perms)
- Non-secret round-trip + caching
- Secret round-trip + encryption-at-rest (ciphertext in DB, plaintext only on .get)
- get_all redaction
- Tamper detection
- migrate_from_env idempotency
- Env-fallback transitional behavior
"""
import base64
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def test_cloud_kek_generated_on_first_use(tmp_cloud_dynamo):
    from mailchad.cloud import settings
    kek = settings._load_kek()
    assert len(kek) == 32
    assert settings.KEK_PATH.exists()
    assert oct(settings.KEK_PATH.stat().st_mode)[-3:] == "600"


def test_cloud_kek_reused_on_second_call(tmp_cloud_dynamo):
    from mailchad.cloud import settings
    k1 = settings._load_kek()
    k2 = settings._load_kek()
    assert k1 == k2


def test_cloud_non_secret_round_trip(tmp_cloud_dynamo):
    from mailchad.cloud import settings
    settings._load_plain_cache()
    settings.set("company_name", "Acme Inc")
    assert settings.get("company_name") == "Acme Inc"


def test_cloud_secret_round_trip_encrypts(tmp_cloud_dynamo):
    from mailchad.cloud import settings
    settings._load_plain_cache()
    settings.set("unsub_secret", "sensitive-value-12345")
    # Round-trip works
    assert settings.get("unsub_secret") == "sensitive-value-12345"
    # And the DynamoDB row stores ciphertext, not plaintext
    from mailchad.cloud import dynamo
    row = dynamo.get_setting("unsub_secret")
    assert row["is_secret"] is True
    assert "sensitive" not in row["value"]
    # Should be base64-decodable to nonce(12) + ciphertext+tag(>=16)
    blob = base64.b64decode(row["value"])
    assert len(blob) >= 28   # 12 nonce + min 16 tag


def test_cloud_get_all_redacts_secrets_by_default(tmp_cloud_dynamo):
    from mailchad.cloud import settings
    settings._load_plain_cache()
    settings.set("company_name", "Acme")
    settings.set("unsub_secret", "shhh")
    out = settings.get_all()
    assert out["company_name"] == "Acme"
    assert out["unsub_secret"] == "<encrypted>"


def test_cloud_get_all_reveals_when_explicit(tmp_cloud_dynamo):
    from mailchad.cloud import settings
    settings._load_plain_cache()
    settings.set("unsub_secret", "shhh")
    out = settings.get_all(redact_secrets=False)
    assert out["unsub_secret"] == "shhh"


def test_cloud_tamper_detection(tmp_cloud_dynamo):
    from mailchad.cloud import settings
    settings._load_plain_cache()
    settings.set("unsub_secret", "original")
    # Tamper: overwrite with garbage ciphertext directly in DynamoDB
    from mailchad.cloud import dynamo
    dynamo.put_setting("unsub_secret", "tampered", is_secret=True)
    # Should fail to decrypt -> return None (with error log)
    assert settings.get("unsub_secret") is None


def test_cloud_migrate_from_env_idempotent(tmp_cloud_dynamo, monkeypatch):
    monkeypatch.setenv("COMPANY_NAME", "FromEnv")
    monkeypatch.setenv("UNSUB_SECRET", "env-secret")
    from mailchad.cloud import settings
    settings._load_plain_cache()

    migrated_1 = settings.migrate_from_env()
    assert "company_name" in migrated_1
    assert "unsub_secret" in migrated_1
    assert settings.get("company_name") == "FromEnv"
    assert settings.get("unsub_secret") == "env-secret"

    # Re-run: nothing new migrates
    migrated_2 = settings.migrate_from_env()
    assert migrated_2 == {}


def test_cloud_env_fallback_when_settings_empty(tmp_cloud_dynamo, monkeypatch):
    """Until migrate_from_env runs, get() should fall back to env for known keys."""
    monkeypatch.setenv("COMPANY_NAME", "FromEnvFallback")
    from mailchad.cloud import settings
    settings._load_plain_cache()
    # Not yet migrated
    assert settings.get("company_name") == "FromEnvFallback"


def test_terminal_kek_separate_from_cloud(tmp_terminal_db):
    from mailchad.terminal import settings as t_settings
    kek = t_settings._load_kek()
    assert len(kek) == 32
    # Different KEYS_DIR, different file, totally independent
    assert t_settings.KEK_PATH != Path("/var/lib/cloud/keys/settings_kek.bin")


def test_terminal_admin_password_hash_is_secret(tmp_terminal_db):
    """Verify admin_password_hash is in SECRET_KEYS - gets encrypted at rest
    even though it's already bcrypt'd (defense in depth)."""
    from mailchad.terminal import settings as t_settings
    assert t_settings.is_secret("admin_password_hash")


def test_terminal_jwt_secret_is_secret(tmp_terminal_db):
    from mailchad.terminal import settings as t_settings
    assert t_settings.is_secret("jwt_secret")


def test_terminal_non_secrets_not_in_secret_keys(tmp_terminal_db):
    """Brand strings should NOT be marked secret (no encryption overhead)."""
    from mailchad.terminal import settings as t_settings
    assert not t_settings.is_secret("company_name")
    assert not t_settings.is_secret("public_host")
    assert not t_settings.is_secret("email_from")
