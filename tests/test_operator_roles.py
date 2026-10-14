"""Role checks on the JSON operator-account mutation routes."""
from __future__ import annotations

import pytest
from sqlalchemy import select


@pytest.fixture
def client(tmp_path, monkeypatch, request):
    from fastapi.testclient import TestClient
    from mailman.terminal import auth, db, encryption, main

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.sqlite")
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setattr(encryption, "KEYS_DIR", keys)
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    db.init()
    session = {"sub": "t@demo.example"}
    if request.param is not None:
        session["role"] = request.param
    main.app.dependency_overrides[auth.require_session] = lambda: session
    c = TestClient(main.app)
    try:
        yield c
    finally:
        main.app.dependency_overrides.pop(auth.require_session, None)
        c.close()


@pytest.mark.parametrize("client", ["operator"], indirect=True)
def test_operator_role_is_refused_on_account_routes(client):
    from mailman.terminal import db, models

    responses = [
        client.post("/admin/operators", json={
            "email": "new@demo.example", "password": "a-long-password-123", "role": "admin",
        }),
        client.post("/admin/operators/1/toggle-active"),
        client.delete("/admin/operators/1"),
    ]
    for response in responses:
        assert response.status_code == 403, response.text
    with db.session() as s:
        assert s.execute(select(models.Operator.id).where(
            models.Operator.email == "new@demo.example"
        )).scalar_one_or_none() is None


@pytest.mark.parametrize("client", ["admin"], indirect=True)
def test_admin_role_manages_accounts(client):
    from mailman.terminal import db, models

    response = client.post("/admin/operators", json={
        "email": "new@demo.example", "password": "a-long-password-123", "role": "admin",
    })
    assert response.status_code == 200, response.text
    op_id = response.json()["id"]
    with db.session() as s:
        assert s.execute(select(models.Operator.email).where(
            models.Operator.id == op_id
        )).scalar_one() == "new@demo.example"
    response = client.post(f"/admin/operators/{op_id}/toggle-active")
    assert response.status_code == 200, response.text
    assert response.json() == {"active": 0}
    response = client.delete(f"/admin/operators/{op_id}")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("client", [None], indirect=True)
def test_role_absent_defaults_to_admin(client):
    response = client.post("/admin/operators", json={
        "email": "new@demo.example", "password": "a-long-password-123", "role": "admin",
    })
    assert response.status_code == 200, response.text
