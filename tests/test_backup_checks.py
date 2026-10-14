"""scripts/_backup.py check_inbox: a distinct row count per inbox kind pins each
predicate of the three counts. Rows are written with raw sqlite3 so the test stays an independent reference."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _backup_mod(tmp_path, monkeypatch):
    """A db.init() DB on tmp_path plus scripts/_backup.py loaded under a unique module name."""
    from mailman.terminal import db
    path = tmp_path / "state.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    spec = importlib.util.spec_from_file_location("znet_backup_under_test", ROOT / "scripts" / "_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def _inbox(path, rows) -> None:
    """rows: (table_name, materialised_at, count). Fills every inbox NOT NULL column without a default."""
    c = sqlite3.connect(str(path))
    try:
        i = 0
        for table_name, materialised_at, n in rows:
            for _ in range(n):
                c.execute("INSERT INTO inbox (table_name, row_id, revision, actor, modified_at, key_id, materialised_at) "
                          "VALUES (?, ?, 1, 'test', '2026-01-01 09:00:00', 'k1', ?)",
                          (table_name, f"znet-{i}", materialised_at))
                i += 1
        c.commit()
    finally:
        c.close()


def test_check_inbox_counts_by_kind(tmp_path, monkeypatch):
    mod, path = _backup_mod(tmp_path, monkeypatch)
    _inbox(path, [("webhook_event", None, 1), ("suppression", None, 2), ("pack", None, 3),
                  ("webhook_event", "2026-01-01 10:00:00", 4), ("other_table", None, 5)])
    # total_inbox 15: every row counts (1 + 2 + 3 + 4 + 5); db.init() seeds no inbox row.
    # unmaterialised 3: webhook_event NULL 1 + suppression NULL 2. pack_pending_outcomes_loop 3: pack NULL.
    assert mod.check_inbox(path) == {
        "total_inbox": 15, "unmaterialised": 3, "pack_pending_outcomes_loop": 3,
        "warning": ("3 webhook/suppression inbox rows not yet materialised - "
                    "wait for materialiser loop before backup for full fidelity")}


def test_check_inbox_no_pending_has_no_warning(tmp_path, monkeypatch):
    mod, path = _backup_mod(tmp_path, monkeypatch)
    _inbox(path, [("webhook_event", "2026-01-01 10:00:00", 2), ("suppression", "2026-01-01 10:00:00", 1),
                  ("pack", None, 2), ("pack", "2026-01-01 10:00:00", 1)])
    assert mod.check_inbox(path) == {"total_inbox": 6, "unmaterialised": 0, "pack_pending_outcomes_loop": 2,
                                     "warning": None}
