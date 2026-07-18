"""Tests for sync protocol - §3, §10.1."""
import base64
import sys
from pathlib import Path

import nacl.public
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent


@pytest.fixture
def client_and_bearers(tmp_cloud_dynamo, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "test-token")
    monkeypatch.setenv("DISABLE_DISPATCHER", "1")
    from mailchad.cloud.main import app
    c = TestClient(app)

    # Register both roles
    op_priv = nacl.public.PrivateKey.generate()
    cl_priv = nacl.public.PrivateKey.generate()
    h = {"X-Bootstrap-Token": "test-token"}
    op_bearer = c.post("/init/handshake",
        json={"role": "operator", "kem_pub": base64.b64encode(bytes(op_priv.public_key)).decode()},
        headers=h).json()["bearer"]
    cl_bearer = c.post("/init/handshake",
        json={"role": "client", "kem_pub": base64.b64encode(bytes(cl_priv.public_key)).decode()},
        headers=h).json()["bearer"]
    return c, op_bearer, cl_bearer


def _event(table: str, row_id: str, revision: int, payload: bytes = b"x",
           modified_at: str = "2026-05-19T01:00:00Z",
           actor: str = "operator") -> dict:
    return {
        "table": table, "row_id": row_id, "revision": revision,
        "actor": actor,
        "modified_at": modified_at,
        "encrypted_payload": base64.b64encode(payload).decode(),
        "key_id": "K_op+K_cl",
        "deleted": False,
    }


def test_push_accepts_events(client_and_bearers):
    c, op_bearer, _ = client_and_bearers
    r = c.post("/sync/push",
        json=[_event("templates", "t1", 1), _event("templates", "t2", 1)],
        headers={"Authorization": f"Bearer {op_bearer}"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 2
    assert len(body["assigned_event_ids"]) == 2
    assert body["near_conflicts"] == []


def test_push_without_bearer_rejected(client_and_bearers):
    c, _, _ = client_and_bearers
    r = c.post("/sync/push", json=[_event("templates", "t1", 1)])
    assert r.status_code == 401


def test_pull_returns_pushed_events(client_and_bearers):
    c, op_bearer, cl_bearer = client_and_bearers
    c.post("/sync/push", json=[_event("templates", "t1", 1)],
           headers={"Authorization": f"Bearer {op_bearer}"})
    r = c.get("/sync/pull?since=0", headers={"Authorization": f"Bearer {cl_bearer}"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["table_name"] == "templates"
    assert body["events"][0]["row_id"] == "t1"


def test_pull_returns_empty_when_no_events(client_and_bearers):
    c, _, cl_bearer = client_and_bearers
    r = c.get("/sync/pull?since=999", headers={"Authorization": f"Bearer {cl_bearer}"})
    assert r.status_code == 200
    assert r.json()["events"] == []


def test_cursor_advances(client_and_bearers):
    c, op_bearer, cl_bearer = client_and_bearers
    push = c.post("/sync/push",
        json=[_event("templates", f"t{i}", 1) for i in range(5)],
        headers={"Authorization": f"Bearer {op_bearer}"})
    max_id = max(push.json()["assigned_event_ids"])

    r = c.get(f"/sync/pull?since={max_id}",
              headers={"Authorization": f"Bearer {cl_bearer}"})
    assert r.json()["events"] == []   # nothing past cursor


def test_near_conflict_detected(client_and_bearers):
    """Two writes to same (table, row_id) within 60s by different actors -> flag."""
    c, op_bearer, cl_bearer = client_and_bearers
    c.post("/sync/push",
        json=[_event("templates", "shared", 1, actor="operator",
                     modified_at="2026-05-19T01:00:00Z")],
        headers={"Authorization": f"Bearer {op_bearer}"})
    r = c.post("/sync/push",
        json=[_event("templates", "shared", 2, actor="client",
                     modified_at="2026-05-19T01:00:30Z")],
        headers={"Authorization": f"Bearer {cl_bearer}"})
    body = r.json()
    assert len(body["near_conflicts"]) == 1
    assert body["near_conflicts"][0]["table"] == "templates"
    assert body["near_conflicts"][0]["delta_s"] == 30.0


def test_no_conflict_when_same_actor(client_and_bearers):
    """Same actor writing twice within 60s ≠ conflict (operator just iterating)."""
    c, op_bearer, _ = client_and_bearers
    c.post("/sync/push",
        json=[_event("templates", "iter", 1, modified_at="2026-05-19T01:00:00Z")],
        headers={"Authorization": f"Bearer {op_bearer}"})
    r = c.post("/sync/push",
        json=[_event("templates", "iter", 2, modified_at="2026-05-19T01:00:30Z")],
        headers={"Authorization": f"Bearer {op_bearer}"})
    assert r.json()["near_conflicts"] == []


def test_actor_mismatch_rejected(client_and_bearers):
    """Bearer says operator but payload claims client -> 403."""
    c, op_bearer, _ = client_and_bearers
    evt = _event("templates", "x", 1)
    evt["actor"] = "client"
    r = c.post("/sync/push", json=[evt],
               headers={"Authorization": f"Bearer {op_bearer}"})
    assert r.status_code == 403


def test_k_temp_set_and_get(client_and_bearers):
    import os
    c, op_bearer, _ = client_and_bearers
    k = os.urandom(32)
    r = c.post("/key/temp",
        json={"k_temp_b64": base64.b64encode(k).decode(), "ttl_seconds": 3600},
        headers={"Authorization": f"Bearer {op_bearer}"})
    assert r.status_code == 200
    assert r.json()["set_by"] == "operator"

    rs = c.get("/key/temp/status", headers={"Authorization": f"Bearer {op_bearer}"})
    s = rs.json()
    assert s["present"] is True
    assert s["ttl_seconds"] == 3600
    assert "k_temp" not in s


def test_k_temp_invalid_ttl_rejected(client_and_bearers):
    import os
    c, op_bearer, _ = client_and_bearers
    r = c.post("/key/temp",
        json={"k_temp_b64": base64.b64encode(os.urandom(32)).decode(), "ttl_seconds": 999},
        headers={"Authorization": f"Bearer {op_bearer}"})
    assert r.status_code == 400


def test_k_temp_wipe(client_and_bearers):
    import os
    c, op_bearer, _ = client_and_bearers
    c.post("/key/temp",
        json={"k_temp_b64": base64.b64encode(os.urandom(32)).decode(), "ttl_seconds": 3600},
        headers={"Authorization": f"Bearer {op_bearer}"})
    r = c.delete("/key/temp", headers={"Authorization": f"Bearer {op_bearer}"})
    assert r.status_code == 200
    s = c.get("/key/temp/status", headers={"Authorization": f"Bearer {op_bearer}"}).json()
    assert s == {"present": False}
