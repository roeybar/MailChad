"""DynamoDB storage layer tests - all run against moto mock, no real AWS."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("tmp_cloud_dynamo")


# counters

def test_counter_increments():
    from mailchad.cloud import dynamo
    a = dynamo._next_id("test_counter")
    b = dynamo._next_id("test_counter")
    assert b == a + 1


def test_independent_counters():
    from mailchad.cloud import dynamo
    a1 = dynamo._next_id("alpha")
    b1 = dynamo._next_id("beta")
    a2 = dynamo._next_id("alpha")
    assert a2 == a1 + 1
    assert b1 == 1


# event_log

def test_put_and_query_events():
    from mailchad.cloud import dynamo
    payload = b"\x01\x02\x03"
    eid = dynamo.put_event(
        table_name="contact", row_id="c1", revision=1,
        actor="operator", modified_at="2026-01-01T00:00:00Z",
        key_id="K_op+K_cl", encrypted_payload=payload, deleted=False,
    )
    assert eid == 1

    events = dynamo.query_events_since(0)
    assert len(events) == 1
    e = events[0]
    assert e["event_id"] == 1
    assert e["table_name"] == "contact"
    assert e["row_id"] == "c1"
    assert e["encrypted_payload"] == payload
    assert e["deleted"] == 0


def test_query_cursor():
    from mailchad.cloud import dynamo
    for i in range(5):
        dynamo.put_event(
            table_name="contact", row_id=f"r{i}", revision=i,
            actor="operator", modified_at="2026-01-01T00:00:00Z",
            key_id="K_op+K_cl", encrypted_payload=None, deleted=False,
        )
    after_3 = dynamo.query_events_since(3)
    assert len(after_3) == 2
    assert after_3[0]["event_id"] == 4


def test_query_latest_event_for_row():
    from mailchad.cloud import dynamo
    dynamo.put_event(
        table_name="campaign", row_id="camp1", revision=1,
        actor="operator", modified_at="2026-01-01T00:00:00Z",
        key_id="K_op+K_cl", encrypted_payload=None, deleted=False,
    )
    dynamo.put_event(
        table_name="campaign", row_id="camp1", revision=2,
        actor="client", modified_at="2026-01-01T00:00:01Z",
        key_id="K_op+K_cl", encrypted_payload=None, deleted=False,
    )
    eid3 = dynamo.put_event(
        table_name="campaign", row_id="camp1", revision=3,
        actor="operator", modified_at="2026-01-01T00:00:02Z",
        key_id="K_op+K_cl", encrypted_payload=None, deleted=False,
    )
    prior = dynamo.query_latest_event_for_row("campaign", "camp1", eid3)
    assert prior is not None
    assert prior["event_id"] == eid3 - 1

    # Different row -> no prior
    none = dynamo.query_latest_event_for_row("campaign", "camp999", 999)
    assert none is None


def test_no_events_returns_empty():
    from mailchad.cloud import dynamo
    assert dynamo.query_events_since(0) == []


# near_conflict

def test_put_and_list_near_conflict():
    from mailchad.cloud import dynamo
    cid = dynamo.put_near_conflict(
        table_name="contact", row_id="c1",
        event_id_a=1, event_id_b=2,
        actor_a="operator", actor_b="client",
        modified_at_a="2026-01-01T00:00:00Z", modified_at_b="2026-01-01T00:00:30Z",
        delta_seconds=30.0,
    )
    assert cid == 1
    conflicts = dynamo.list_near_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["id"] == 1
    assert conflicts[0]["delta_seconds"] == 30.0


def test_ack_near_conflict():
    from mailchad.cloud import dynamo
    cid = dynamo.put_near_conflict(
        table_name="t", row_id="r", event_id_a=1, event_id_b=2,
        actor_a="operator", actor_b="client",
        modified_at_a="2026-01-01T00:00:00Z", modified_at_b="2026-01-01T00:00:30Z",
        delta_seconds=30.0,
    )
    ok = dynamo.ack_near_conflict(cid, "operator")
    assert ok
    conflicts = dynamo.list_near_conflicts(unacked_only=True)
    assert len(conflicts) == 0


# sessions

def test_session_lifecycle():
    from mailchad.cloud import dynamo
    dynamo.put_session("bearhash1", "operator")
    sess = dynamo.get_session_by_bearer("bearhash1")
    assert sess is not None
    assert sess["actor"] == "operator"
    assert sess["revoked_at"] is None


def test_session_not_found():
    from mailchad.cloud import dynamo
    assert dynamo.get_session_by_bearer("nonexistent") is None


def test_touch_session():
    from mailchad.cloud import dynamo
    dynamo.put_session("bh2", "client")
    dynamo.touch_session("bh2")
    sess = dynamo.get_session_by_bearer("bh2")
    assert sess["last_seen_at"] is not None


def test_revoke_sessions_for_actor():
    from mailchad.cloud import dynamo
    dynamo.put_session("bh3", "operator")
    dynamo.put_session("bh4", "operator")
    dynamo.put_session("bh5", "client")
    count = dynamo.revoke_sessions_for_actor("operator")
    assert count == 2
    s3 = dynamo.get_session_by_bearer("bh3")
    assert s3["revoked_at"] is not None
    s5 = dynamo.get_session_by_bearer("bh5")
    assert s5["revoked_at"] is None


# pubkeys

def test_pubkey_lifecycle():
    from mailchad.cloud import dynamo
    pub = b"\xAA" * 32
    dynamo.put_pubkey("operator", pub)
    fetched = dynamo.get_pubkey("operator")
    assert fetched == pub


def test_pubkey_not_found():
    from mailchad.cloud import dynamo
    assert dynamo.get_pubkey("operator") is None


def test_get_all_pubkeys():
    from mailchad.cloud import dynamo
    dynamo.put_pubkey("operator", b"\xAA" * 32)
    dynamo.put_pubkey("client", b"\xBB" * 32)
    all_keys = dynamo.get_all_pubkeys()
    assert set(all_keys.keys()) == {"operator", "client"}


def test_delete_pubkey():
    from mailchad.cloud import dynamo
    dynamo.put_pubkey("operator", b"\xAA" * 32)
    dynamo.delete_pubkey("operator")
    assert dynamo.get_pubkey("operator") is None


def test_list_pubkeys():
    from mailchad.cloud import dynamo
    dynamo.put_pubkey("operator", b"\xAA" * 32)
    rows = dynamo.list_pubkeys()
    assert len(rows) == 1
    assert rows[0]["actor"] == "operator"


# packs

def test_put_and_get_pack():
    from mailchad.cloud import dynamo
    dynamo.put_pack(
        pack_id="p1", campaign_id=1, recipient_hash="r1", content_hash="c1",
        send_at="2026-01-01T00:00:00Z", key_id="kid1",
        encrypted_payload=b"\x01\x02", pushed_by="operator",
    )
    pack = dynamo.get_pack("p1")
    assert pack is not None
    assert pack["pack_id"] == "p1"
    assert pack["status"] == "pending"
    assert pack["encrypted_payload"] == b"\x01\x02"


def test_put_pack_duplicate_raises():
    from mailchad.cloud import dynamo
    dynamo.put_pack(
        pack_id="p2", campaign_id=1, recipient_hash="r1", content_hash="c1",
        send_at="2026-01-01T00:00:00Z", key_id="kid1",
        encrypted_payload=b"\x01", pushed_by="operator",
    )
    with pytest.raises(dynamo.PackAlreadyExists):
        dynamo.put_pack(
            pack_id="p2", campaign_id=1, recipient_hash="r1", content_hash="c1",
            send_at="2026-01-01T00:00:00Z", key_id="kid1",
            encrypted_payload=b"\x01", pushed_by="operator",
        )


def test_claim_pack():
    from mailchad.cloud import dynamo
    dynamo.put_pack(
        pack_id="p3", campaign_id=1, recipient_hash="r", content_hash="c",
        send_at="2026-01-01T00:00:00Z", key_id="k",
        encrypted_payload=b"\x01", pushed_by="operator",
    )
    ok = dynamo.claim_pack("p3")
    assert ok
    # Second claim should fail (already claimed)
    ok2 = dynamo.claim_pack("p3")
    assert not ok2


def test_update_pack():
    from mailchad.cloud import dynamo
    dynamo.put_pack(
        pack_id="p4", campaign_id=1, recipient_hash="r", content_hash="c",
        send_at="2026-01-01T00:00:00Z", key_id="k",
        encrypted_payload=b"\x01", pushed_by="operator",
    )
    dynamo.update_pack("p4", status="sent", resend_message_id="msg123")
    pack = dynamo.get_pack("p4")
    assert pack["status"] == "sent"
    assert pack["resend_message_id"] == "msg123"


def test_list_pending_packs():
    from mailchad.cloud import dynamo
    dynamo.put_pack(
        pack_id="p5", campaign_id=1, recipient_hash="r", content_hash="c",
        send_at="2025-01-01T00:00:00Z", key_id="k",
        encrypted_payload=b"\x01", pushed_by="operator",
    )
    dynamo.put_pack(
        pack_id="p6", campaign_id=1, recipient_hash="r", content_hash="c",
        send_at="2025-01-01T00:01:00Z", key_id="k",
        encrypted_payload=b"\x01", pushed_by="operator",
    )
    pending = dynamo.list_pending_packs()
    pack_ids = {p["pack_id"] for p in pending}
    assert "p5" in pack_ids
    assert "p6" in pack_ids


# webhooks

def test_webhook_dedup():
    from mailchad.cloud import dynamo
    dynamo.put_webhook_raw(svix_id="sv1", event_type="email.delivered", message_id="m1")
    row = dynamo.get_webhook_by_svix("sv1")
    assert row is not None
    assert row["svix_id"] == "sv1"


def test_webhook_not_found():
    from mailchad.cloud import dynamo
    assert dynamo.get_webhook_by_svix("nope") is None


# settings

def test_settings_put_get():
    from mailchad.cloud import dynamo
    dynamo.put_setting("company_name", "ACME Corp", is_secret=False)
    row = dynamo.get_setting("company_name")
    assert row is not None
    assert row["value"] == "ACME Corp"
    assert row["is_secret"] is False


def test_settings_put_overwrite():
    from mailchad.cloud import dynamo
    dynamo.put_setting("company_name", "Old", is_secret=False)
    dynamo.put_setting("company_name", "New", is_secret=False)
    row = dynamo.get_setting("company_name")
    assert row["value"] == "New"


def test_settings_delete():
    from mailchad.cloud import dynamo
    dynamo.put_setting("company_name", "ACME", is_secret=False)
    dynamo.delete_setting("company_name")
    assert dynamo.get_setting("company_name") is None


def test_settings_list():
    from mailchad.cloud import dynamo
    dynamo.put_setting("k1", "v1", is_secret=False)
    dynamo.put_setting("k2", "v2", is_secret=True)
    rows = dynamo.list_settings()
    keys = {r["key"] for r in rows}
    assert "k1" in keys
    assert "k2" in keys


def test_setting_not_found():
    from mailchad.cloud import dynamo
    assert dynamo.get_setting("nosuchkey") is None


# compliance cache

def test_unsub_new_and_duplicate():
    from mailchad.cloud import dynamo
    is_new = dynamo.put_unsub("hash1", "tok1")
    assert is_new
    is_dup = dynamo.put_unsub("hash1", "tok1")
    assert not is_dup


def test_erasure_put():
    from mailchad.cloud import dynamo
    dynamo.put_erasure("hash2", "tok2")  # should not raise


# keys_dynamo

def test_ktemp_lifecycle():
    from mailchad.cloud import keys_dynamo
    k = b"\xBB" * 32
    meta = keys_dynamo.set_k_temp(k, 3600, "operator")
    assert meta["ttl_seconds"] == 3600
    assert len(meta["key_id"]) == 8

    fetched = keys_dynamo.get_active_k_temp()
    assert fetched == k

    status = keys_dynamo.k_temp_status()
    assert status["present"]
    assert status["remaining_s"] > 0

    keys_dynamo.wipe_k_temp()
    assert keys_dynamo.get_active_k_temp() is None
    assert not keys_dynamo.k_temp_status()["present"]


def test_ktemp_bad_size():
    from mailchad.cloud import keys_dynamo
    with pytest.raises(ValueError, match="32 bytes"):
        keys_dynamo.set_k_temp(b"\x00" * 16, 3600, "operator")


def test_ktemp_bad_ttl():
    from mailchad.cloud import keys_dynamo
    with pytest.raises(keys_dynamo.TTLViolation):
        keys_dynamo.set_k_temp(b"\x00" * 32, 9999, "operator")


def test_ktemp_no_key_returns_none():
    from mailchad.cloud import keys_dynamo
    assert keys_dynamo.get_active_k_temp() is None
