"""routes_admin.edit_contact: an edit touches only the row it names.
Called directly with routes_admin.ContactEditIn on a
db.init() DB seeded with raw sqlite3."""
from __future__ import annotations

import sqlite3

import pytest

CONTACTS = [("a@example.com", "Ann", "vip", "2026-01-01 09:00:00", "import", "ext-1"),
            ("b@example.com", "Bob", "lead", "2026-01-02 09:00:00", "form", None),
            ("c@example.com", None, None, "2026-01-03 09:00:00", "api", "ext-3")]


def _rows(path) -> list[dict]:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM contacts ORDER BY id")]
    finally:
        c.close()


@pytest.fixture
def admin(tmp_path, monkeypatch):
    from mailman.terminal import db, routes_admin
    path = tmp_path / "state.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    c = sqlite3.connect(str(path))
    try:
        c.executemany("INSERT INTO contacts (email, name, tags, consent_ts, consent_source, external_id) "
                      "VALUES (?, ?, ?, ?, ?, ?)", CONTACTS)
        c.commit()
    finally:
        c.close()
    return routes_admin, path


def test_edit_contact_changes_only_that_row(admin):
    m, path = admin
    before = _rows(path)
    assert [r["id"] for r in before] == [1, 2, 3]
    assert m.edit_contact(2, m.ContactEditIn(name="Ed", tags="x")) == {"updated": True}
    after = _rows(path)
    assert after[1] == dict(before[1], name="Ed", tags="x")
    assert [after[0], after[2]] == [before[0], before[2]]
    assert len(after) == 3


def test_edit_contact_missing_id_is_404(admin):
    from fastapi import HTTPException
    m, path = admin
    before = _rows(path)
    with pytest.raises(HTTPException) as ei:
        m.edit_contact(999, m.ContactEditIn(name="n"))
    assert (ei.value.status_code, ei.value.detail) == (404, "no such contact")
    assert _rows(path) == before
