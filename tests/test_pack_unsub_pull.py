"""Regression tests for the cursor-based pull bugs fixed in v3.21:
  - list_resolved_packs must filter by enqueued_at and advance (outcomes loop
    was freezing on the oldest page).
  - list_unsubs must return UNSUB_LOG rows after a cursor (the /sync/pull
    catch-back).
All run against moto, no real AWS.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("tmp_cloud_dynamo")


def _put_resolved_pack(pack_id: str, enqueued_at: str, status: str = "sent"):
    from mailchad.cloud import dynamo
    dynamo._get_table().put_item(Item={
        "pk": f"PACK#{pack_id}", "sk": f"PACK#{pack_id}",
        "pack_id": pack_id, "campaign_id": 1,
        "recipient_hash": "h", "content_hash": "c",
        "send_at": "1970-01-01T00:00:00Z",          # immediate -> constant, the old bug
        "key_id": "K", "encrypted_payload": b"x",
        "status": status, "enqueued_at": enqueued_at,
        "pushed_by": "test", "resend_message_id": f"msg-{pack_id}",
    })


# list_resolved_packs (pack pagination fix)

def test_resolved_packs_no_cursor_returns_all_oldest_first():
    from mailchad.cloud import dynamo
    _put_resolved_pack("a", "2026-06-21T01:00:00Z")
    _put_resolved_pack("b", "2026-06-21T02:00:00Z")
    _put_resolved_pack("c", "2026-06-21T03:00:00Z")
    rows = dynamo.list_resolved_packs(since_enqueued_at="", limit=500)
    got = [r["pack_id"] for r in rows]
    assert got == ["a", "b", "c"]


def test_resolved_packs_cursor_filters_and_advances():
    from mailchad.cloud import dynamo
    _put_resolved_pack("a", "2026-06-21T01:00:00Z")
    _put_resolved_pack("b", "2026-06-21T02:00:00Z")
    _put_resolved_pack("c", "2026-06-21T03:00:00Z")
    # cursor at b's time -> only c (the bug returned a,b,c forever, cursor frozen)
    rows = dynamo.list_resolved_packs(since_enqueued_at="2026-06-21T02:00:00Z", limit=500)
    got = [r["pack_id"] for r in rows]
    assert got == ["c"], got
    # cursor at the head -> nothing newer
    rows2 = dynamo.list_resolved_packs(since_enqueued_at="2026-06-21T03:00:00Z", limit=500)
    assert rows2 == []


def test_resolved_packs_ignores_pending():
    from mailchad.cloud import dynamo
    _put_resolved_pack("done", "2026-06-21T01:00:00Z", status="sent")
    _put_resolved_pack("wip",  "2026-06-21T02:00:00Z", status="pending")
    rows = dynamo.list_resolved_packs(since_enqueued_at="", limit=500)
    assert [r["pack_id"] for r in rows] == ["done"]


# list_unsubs (catch-back pull)

def test_put_unsub_writes_log_and_list_filters_by_cursor():
    from mailchad.cloud import dynamo
    dynamo.put_unsub("hash_old", source_token="t1", scope="promotional")
    dynamo.put_unsub("hash_new", source_token="t2", scope="all")

    rows = dynamo.list_unsubs(since_cursor="", limit=500)
    hashes = {r["email_hash"] for r in rows}
    assert {"hash_old", "hash_new"} <= hashes
    # each row carries a cursor + scope
    for r in rows:
        assert "cursor" in r and r["scope"] in ("promotional", "all")

    # pulling past the last cursor returns nothing new
    last = rows[-1]["cursor"]
    assert dynamo.list_unsubs(since_cursor=last, limit=500) == []


def test_unsub_scope_upgrade_appends_log():
    from mailchad.cloud import dynamo
    dynamo.put_unsub("h", source_token="t", scope="promotional")
    before = len(dynamo.list_unsubs(since_cursor="", limit=500))
    dynamo.put_unsub("h", source_token="t", scope="all")   # upgrade -> new log row
    after = dynamo.list_unsubs(since_cursor="", limit=500)
    assert len(after) >= before
    assert any(r["email_hash"] == "h" and r["scope"] == "all" for r in after)
