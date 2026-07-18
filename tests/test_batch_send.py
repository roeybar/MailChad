"""v3.22 - send-schedule (window/senders/rush) + batch drain/cooldown unit tests.
No cloud, no real AWS. send_schedule is a pure function; drain runs on a temp DB.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from mailchad.terminal import send_schedule as ss
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def _to_pt(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(PT)


# send_schedule

def test_all_sends_inside_window():
    start = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    ats = ss.compute_send_ats(500, start, seed=1, start_hour=9, window_hours=4)
    for a in ats:
        d = _to_pt(a)
        assert 9 <= d.hour < 13, f"{a} -> {d} outside 09:00-13:00 PT"


def test_sorted_and_count():
    start = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    ats = ss.compute_send_ats(300, start, seed=2)
    assert len(ats) == 300
    assert ats == sorted(ats)


def test_multi_day_spread():
    start = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    # 3 senders, ~600/day -> 2000 needs ≥3 days
    ats = ss.compute_send_ats(2000, start, seed=3)
    days = {_to_pt(a).date() for a in ats}
    assert len(days) >= 3, f"expected multi-day spread, got {len(days)} day(s)"


def test_rush_densifies_tail():
    start = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    ats = [ _to_pt(a) for a in ss.compute_send_ats(1500, start, seed=4,
            start_hour=9, window_hours=4, rush_tail_minutes=30) ]
    day1 = [d for d in ats if d.date() == ats[0].date()]
    # last 30 min (12:30-13:00) vs an equal mid-window slice (10:00-10:30)
    rush = [d for d in day1 if d.hour == 12 and d.minute >= 30]
    mid  = [d for d in day1 if d.hour == 10 and d.minute < 30]
    assert len(rush) > len(mid), f"rush {len(rush)} should exceed mid {len(mid)}"


def test_starts_at_window_open_when_before():
    start = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)  # midnight UTC = before PT window
    ats = ss.compute_send_ats(5, start, seed=5, start_hour=9)
    first = _to_pt(ats[0])
    assert first.hour == 9 and first.minute == 0


def test_next_window_open_after():
    # during a window -> next open is tomorrow
    midwin = datetime(2026, 6, 22, 17, 30, tzinfo=UTC)  # ~10:30 PT
    nxt = _to_pt(ss.next_window_open_after(midwin, start_hour=9))
    assert nxt.hour == 9 and nxt.date() > _to_pt("2026-06-22T17:30:00Z").date()


# batch drain -> cooldown

def _make_db(path):
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE campaign_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INT, batch_no INT,
        size INT, status TEXT, window_start TEXT, window_end TEXT,
        loaded_at TEXT, drained_at TEXT, approve_unlock_at TEXT, approved_at TEXT);
      CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, is_secret INTEGER DEFAULT 0);
    """)
    c.commit(); return c


def test_compliance_token_substitution_in_body():
    """Real-world templates often place {{unsubscribeUrl}} in their own body; it must
    be substituted, and our footer must NOT also be appended (no double unsub)."""
    from mailchad.terminal import launch
    with patch.object(launch.settings, "get", lambda k, d=None: "123 St" if k == "email_footer_address" else d):
        html = "<p>Hi</p><a href='{{unsubscribeUrl}}'>unsub</a> <a href='{{erasureUrl}}'>del</a>"
        out_html, out_text = launch._inject_compliance(html, "", "https://x/u/T", "https://x/e/T", "promotional")
    assert "{{unsubscribeUrl}}" not in out_html and "{{erasureUrl}}" not in out_html
    assert "https://x/u/T" in out_html and "https://x/e/T" in out_html
    assert out_html.count("nsubscribe") + out_html.count("unsub") <= 2   # no appended footer dup


def test_compliance_footer_appended_when_no_token():
    from mailchad.terminal import launch
    with patch.object(launch.settings, "get", lambda k, d=None: "123 St" if k == "email_footer_address" else d):
        out_html, out_text = launch._inject_compliance("<p>Hi</p>", "Hi", "https://x/u/T", "https://x/e/T", "promotional")
    assert "https://x/u/T" in out_html      # footer appended since template had no token
    assert "123 St" in out_html


def test_drain_transitions_sending_to_drained(tmp_path):
    import mailchad.terminal.db as _db, mailchad.terminal.sync_client as sc
    p = str(tmp_path / "b.sqlite"); c = _make_db(p)
    past = "2026-01-01T00:00:00Z"
    c.execute("INSERT INTO campaign_batches (campaign_id,batch_no,size,status,window_end) "
              "VALUES (1,1,10,'sending',?)", (past,))
    # a still-running batch (window_end in far future) must NOT drain
    c.execute("INSERT INTO campaign_batches (campaign_id,batch_no,size,status,window_end) "
              "VALUES (1,2,10,'sending','2999-01-01T00:00:00Z')")
    c.commit()
    with patch.object(_db, "DB_PATH", Path(p)):
        sc._drain_batches()
    c2 = sqlite3.connect(p); c2.row_factory = sqlite3.Row
    rows = {r["batch_no"]: r for r in c2.execute("SELECT * FROM campaign_batches")}
    assert rows[1]["status"] == "drained"
    assert rows[1]["drained_at"] is not None
    assert rows[1]["approve_unlock_at"] is not None       # cooldown gate set
    assert rows[2]["status"] == "sending"                 # not yet due
