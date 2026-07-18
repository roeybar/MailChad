"""Tests for init handshake endpoint - §7, §13.7."""
import base64
import importlib.util
import sys
from pathlib import Path

import nacl.public
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent


@pytest.fixture
def client(tmp_cloud_dynamo, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "test-token-secret-for-pytest")
    monkeypatch.setenv("DISABLE_DISPATCHER", "1")
    # tmp_cloud_dynamo already sets sys.path + calls dynamo.init()
    from mailchad.cloud.main import app
    return TestClient(app)


def _pub_b64() -> tuple[nacl.public.PrivateKey, str]:
    p = nacl.public.PrivateKey.generate()
    return p, base64.b64encode(bytes(p.public_key)).decode()


def test_handshake_status_initially_empty(client):
    r = client.get("/init/handshake/status")
    assert r.status_code == 200
    assert r.json()["registered_roles"] == []


def test_operator_registers_successfully(client):
    _, pub = _pub_b64()
    r = client.post(
        "/init/handshake",
        json={"role": "operator", "kem_pub": pub},
        headers={"X-Bootstrap-Token": "test-token-secret-for-pytest"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] == "operator"
    assert "bearer" in body
    assert len(body["bearer"]) > 40
    assert len(body["fingerprint"]) == 16


def test_bad_bootstrap_rejected(client):
    _, pub = _pub_b64()
    r = client.post(
        "/init/handshake",
        json={"role": "operator", "kem_pub": pub},
        headers={"X-Bootstrap-Token": "wrong-token"},
    )
    assert r.status_code == 401


def test_missing_bootstrap_rejected(client):
    _, pub = _pub_b64()
    r = client.post(
        "/init/handshake",
        json={"role": "operator", "kem_pub": pub},
    )
    assert r.status_code == 401


def test_re_register_same_role_rejected(client):
    _, pub = _pub_b64()
    h = {"X-Bootstrap-Token": "test-token-secret-for-pytest"}
    client.post("/init/handshake", json={"role": "operator", "kem_pub": pub}, headers=h)
    r2 = client.post("/init/handshake", json={"role": "operator", "kem_pub": pub}, headers=h)
    assert r2.status_code == 409


def test_revoke_allows_re_register(client):
    _, pub = _pub_b64()
    h = {"X-Bootstrap-Token": "test-token-secret-for-pytest"}
    client.post("/init/handshake", json={"role": "operator", "kem_pub": pub}, headers=h)
    rd = client.delete("/init/handshake/operator", headers=h)
    assert rd.status_code == 200
    r2 = client.post("/init/handshake", json={"role": "operator", "kem_pub": pub}, headers=h)
    assert r2.status_code == 200


def test_bad_role_rejected(client):
    _, pub = _pub_b64()
    r = client.post(
        "/init/handshake",
        json={"role": "admin", "kem_pub": pub},
        headers={"X-Bootstrap-Token": "test-token-secret-for-pytest"},
    )
    assert r.status_code == 400


def test_bad_pubkey_size_rejected(client):
    r = client.post(
        "/init/handshake",
        json={"role": "operator", "kem_pub": base64.b64encode(b"too short").decode()},
        headers={"X-Bootstrap-Token": "test-token-secret-for-pytest"},
    )
    assert r.status_code == 400


def test_pubkeys_endpoint_returns_registered(client):
    _, op_pub = _pub_b64()
    _, cl_pub = _pub_b64()
    h = {"X-Bootstrap-Token": "test-token-secret-for-pytest"}
    client.post("/init/handshake", json={"role": "operator", "kem_pub": op_pub}, headers=h)
    client.post("/init/handshake", json={"role": "client", "kem_pub": cl_pub}, headers=h)
    r = client.get("/pubkeys")
    body = r.json()
    assert body["operator"] == op_pub
    assert body["client"] == cl_pub
