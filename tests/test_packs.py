"""Tests for pack lifecycle - §4, §10.3."""
import base64
import json
import os
import sys
import uuid
from pathlib import Path

import nacl.public
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent


@pytest.fixture
def client_and_bearer(tmp_cloud_dynamo, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "tt")
    monkeypatch.setenv("DISABLE_DISPATCHER", "1")
    from mailchad.cloud.main import app
    c = TestClient(app)
    op_priv = nacl.public.PrivateKey.generate()
    bearer = c.post("/init/handshake",
        json={"role": "operator", "kem_pub": base64.b64encode(bytes(op_priv.public_key)).decode()},
        headers={"X-Bootstrap-Token": "tt"}).json()["bearer"]
    return c, bearer


def _build_pack(k_temp: bytes, recipient: str = "x@example.com") -> dict:
    """Build a pack encrypted with K_temp matching the on-the-wire shape."""
    sys.path.insert(0, str(ROOT / "terminal"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("term_enc", ROOT / "src/mailchad/terminal/encryption.py")
    te = importlib.util.module_from_spec(spec); spec.loader.exec_module(te)
    plain = json.dumps({
        "recipient": recipient, "subject": "test", "html": "<p>x</p>",
        "text": "x", "headers": {}, "from": "noreply@x.com",
        "resend_api_key": "re_test",
    }).encode()
    env = te.encrypt_with_temp(plain, k_temp)
    return {
        "pack_id":        str(uuid.uuid4()),
        "campaign_id":    1,
        "recipient_hash": "0" * 64,
        "content_hash":   "1" * 64,
        "send_at":        "1970-01-01T00:00:00Z",
        "key_id":         f"K_temp_{te.k_temp_id(k_temp)}",
        "encrypted_blob": base64.b64encode(env).decode(),
    }


def test_push_pack(client_and_bearer):
    c, bearer = client_and_bearer
    k = os.urandom(32)
    c.post("/key/temp", json={"k_temp_b64": base64.b64encode(k).decode(), "ttl_seconds": 3600},
           headers={"Authorization": f"Bearer {bearer}"})
    pack = _build_pack(k)
    r = c.post("/packs", json=[pack], headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 200
    assert r.json()["accepted"] == 1


def test_push_pack_idempotent(client_and_bearer):
    c, bearer = client_and_bearer
    k = os.urandom(32)
    c.post("/key/temp", json={"k_temp_b64": base64.b64encode(k).decode(), "ttl_seconds": 3600},
           headers={"Authorization": f"Bearer {bearer}"})
    pack = _build_pack(k)
    c.post("/packs", json=[pack], headers={"Authorization": f"Bearer {bearer}"})
    r2 = c.post("/packs", json=[pack], headers={"Authorization": f"Bearer {bearer}"})
    assert r2.json()["accepted"] == 0   # idempotent on pack_id


def test_cancel_pending_pack(client_and_bearer):
    c, bearer = client_and_bearer
    k = os.urandom(32)
    c.post("/key/temp", json={"k_temp_b64": base64.b64encode(k).decode(), "ttl_seconds": 3600},
           headers={"Authorization": f"Bearer {bearer}"})
    pack = _build_pack(k)
    c.post("/packs", json=[pack], headers={"Authorization": f"Bearer {bearer}"})
    r = c.delete(f"/packs/{pack['pack_id']}", headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 200
    assert r.json()["cancelled"] is True


def test_cancel_unknown_pack_404(client_and_bearer):
    c, bearer = client_and_bearer
    r = c.delete("/packs/nonexistent", headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 404


def test_push_pack_without_bearer_rejected(client_and_bearer):
    c, _ = client_and_bearer
    r = c.post("/packs", json=[])
    assert r.status_code == 401
