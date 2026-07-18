"""DynamoDB storage layer - v3.2 (replaces db.py / SQLite).

Single table, on-demand billing. All entities share one table keyed by
(pk, sk). Two GSIs handle the two non-primary query patterns:

  row-key-index   PK=row_key  SK=sk   -> near-conflict detection
  status-index    PK=status   SK=send_at -> pending pack dispatch

Integer event_id / conflict_id maintained via atomic counter items so
the terminal sync protocol (which carries integer cursors) needs no change.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

log = logging.getLogger("cloud.dynamo")

_resource: "boto3.resources.base.ServiceResource | None" = None
_table: "boto3.resources.base.ServiceResource | None" = None


# client init

def _get_resource():
    global _resource
    if _resource is None:
        kwargs: dict = {"region_name": os.environ.get("AWS_REGION", "us-east-1")}
        endpoint = os.environ.get("DYNAMODB_ENDPOINT")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        _resource = boto3.resource("dynamodb", **kwargs)
    return _resource


def _get_table():
    global _table
    if _table is None:
        name = os.environ.get("DYNAMODB_TABLE", "ep-v3-dev")
        _table = _get_resource().Table(name)
    return _table


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reset_clients() -> None:
    """For tests - moto needs a fresh resource after mock restart."""
    global _resource, _table
    _resource = None
    _table = None


# table init

def init() -> None:
    """Create table + GSIs if they don't exist. Idempotent."""
    table_name = os.environ.get("DYNAMODB_TABLE", "ep-v3-dev")
    resource = _get_resource()
    try:
        resource.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk",      "AttributeType": "S"},
                {"AttributeName": "sk",      "AttributeType": "S"},
                {"AttributeName": "row_key", "AttributeType": "S"},
                {"AttributeName": "status",  "AttributeType": "S"},
                {"AttributeName": "send_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "row-key-index",
                    "KeySchema": [
                        {"AttributeName": "row_key", "KeyType": "HASH"},
                        {"AttributeName": "sk",      "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {"AttributeName": "status",  "KeyType": "HASH"},
                        {"AttributeName": "send_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        log.info("dynamo: created table %s", table_name)
        # Wait until table is active (local/moto are immediate; real AWS needs a moment)
        _get_resource().Table(table_name).wait_until_exists()
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
        log.debug("dynamo: table %s already exists", table_name)
    # Enable TTL (no-op on DynamoDB Local and moto, important on real AWS)
    try:
        client = boto3.client(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            **( {"endpoint_url": os.environ.get("DYNAMODB_ENDPOINT")}
                if os.environ.get("DYNAMODB_ENDPOINT") else {} ),
        )
        client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
    except Exception:
        pass  # DynamoDB Local ignores TTL; moto supports it


# atomic counter

def _next_id(name: str) -> int:
    """Atomic increment of counter `name`. Returns the new integer value."""
    resp = _get_table().update_item(
        Key={"pk": "COUNTER", "sk": name},
        UpdateExpression="ADD seq :one",
        ExpressionAttributeValues={":one": Decimal("1")},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["seq"])


# event_log

def put_event(
    *,
    table_name: str,
    row_id: str,
    revision: int,
    actor: str,
    modified_at: str,
    key_id: str,
    encrypted_payload: bytes | None,
    deleted: bool,
) -> int:
    """Append a sync event. Returns its integer event_id."""
    event_id = _next_id("event_log")
    sk = f"{event_id:010d}"
    item: dict = {
        "pk":                "EVENT",
        "sk":                sk,
        "event_id":          event_id,
        "table_name":        table_name,
        "row_id":            row_id,
        "revision":          revision,
        "actor":             actor,
        "modified_at":       modified_at,
        "key_id":            key_id,
        "deleted":           1 if deleted else 0,
        "received_at":       _now(),
        "row_key":           f"{table_name}#{row_id}",
    }
    if encrypted_payload is not None:
        item["encrypted_payload"] = bytes(encrypted_payload)
    _get_table().put_item(Item=item)
    return event_id


def query_events_since(since_id: int, limit: int = 500) -> list[dict]:
    """Return events with event_id > since_id, ordered ascending, up to limit."""
    since_sk = f"{since_id:010d}"
    resp = _get_table().query(
        KeyConditionExpression=(
            Key("pk").eq("EVENT") & Key("sk").gt(since_sk)
        ),
        Limit=limit,
        ScanIndexForward=True,
    )
    return [_coerce_event(item) for item in resp.get("Items", [])]


def query_latest_event_for_row(table_name: str, row_id: str, before_id: int) -> dict | None:
    """Return the most recent event for (table_name, row_id) with event_id < before_id.
    Used by near-conflict detection."""
    before_sk = f"{before_id:010d}"
    resp = _get_table().query(
        IndexName="row-key-index",
        KeyConditionExpression=(
            Key("row_key").eq(f"{table_name}#{row_id}") & Key("sk").lt(before_sk)
        ),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    return _coerce_event(items[0]) if items else None


def _coerce_event(item: dict) -> dict:
    payload = item.get("encrypted_payload")
    return {
        "event_id":          int(item["event_id"]),
        "table_name":        item["table_name"],
        "row_id":            item["row_id"],
        "revision":          int(item["revision"]),
        "actor":             item["actor"],
        "modified_at":       item["modified_at"],
        "key_id":            item["key_id"],
        "encrypted_payload": bytes(payload) if payload is not None else None,
        "deleted":           int(item.get("deleted", 0)),
        "received_at":       item.get("received_at", ""),
    }


# near_conflict_log

def put_near_conflict(
    *,
    table_name: str,
    row_id: str,
    event_id_a: int,
    event_id_b: int,
    actor_a: str,
    actor_b: str,
    modified_at_a: str,
    modified_at_b: str,
    delta_seconds: float,
) -> int:
    conflict_id = _next_id("near_conflict")
    _get_table().put_item(Item={
        "pk":            "CONFLICT",
        "sk":            f"{conflict_id:010d}",
        "conflict_id":   conflict_id,
        "table_name":    table_name,
        "row_id":        row_id,
        "event_id_a":    event_id_a,
        "event_id_b":    event_id_b,
        "actor_a":       actor_a,
        "actor_b":       actor_b,
        "modified_at_a": modified_at_a,
        "modified_at_b": modified_at_b,
        "delta_seconds": str(delta_seconds),
        "detected_at":   _now(),
    })
    return conflict_id


def list_near_conflicts(*, unacked_only: bool = False, limit: int = 200) -> list[dict]:
    resp = _get_table().query(
        KeyConditionExpression=Key("pk").eq("CONFLICT"),
        Limit=limit,
        ScanIndexForward=False,
    )
    items = [_coerce_conflict(i) for i in resp.get("Items", [])]
    if unacked_only:
        items = [i for i in items if not i.get("acknowledged_at")]
    return items


def ack_near_conflict(conflict_id: int, actor: str) -> bool:
    sk = f"{conflict_id:010d}"
    try:
        _get_table().update_item(
            Key={"pk": "CONFLICT", "sk": sk},
            UpdateExpression="SET acknowledged_at = :t, acknowledged_by = :a",
            ConditionExpression=Attr("conflict_id").exists(),
            ExpressionAttributeValues={":t": _now(), ":a": actor},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _coerce_conflict(item: dict) -> dict:
    return {
        "id":             int(item["conflict_id"]),
        "table_name":     item["table_name"],
        "row_id":         item["row_id"],
        "event_id_a":     int(item["event_id_a"]),
        "event_id_b":     int(item["event_id_b"]),
        "actor_a":        item["actor_a"],
        "actor_b":        item["actor_b"],
        "modified_at_a":  item["modified_at_a"],
        "modified_at_b":  item["modified_at_b"],
        "delta_seconds":  float(item["delta_seconds"]),
        "detected_at":    item["detected_at"],
        "acknowledged_at": item.get("acknowledged_at"),
        "acknowledged_by": item.get("acknowledged_by"),
    }


# terminal_session

def put_session(bearer_hash: str, actor: str) -> None:
    _get_table().put_item(Item={
        "pk":         f"SESSION#{bearer_hash}",
        "sk":         f"SESSION#{bearer_hash}",
        "bearer_hash": bearer_hash,
        "actor":      actor,
        "created_at": _now(),
        "last_seen_at": None,
        "revoked_at": None,
    })


def get_session_by_bearer(bearer_hash: str) -> dict | None:
    resp = _get_table().get_item(Key={
        "pk": f"SESSION#{bearer_hash}",
        "sk": f"SESSION#{bearer_hash}",
    })
    item = resp.get("Item")
    return _coerce_session(item) if item else None


def touch_session(bearer_hash: str) -> None:
    try:
        _get_table().update_item(
            Key={"pk": f"SESSION#{bearer_hash}", "sk": f"SESSION#{bearer_hash}"},
            UpdateExpression="SET last_seen_at = :t",
            ExpressionAttributeValues={":t": _now()},
        )
    except Exception:
        pass


def revoke_sessions_for_actor(actor: str) -> int:
    """Revoke all active sessions for actor. Returns count revoked."""
    # Scan is needed here since we don't have actor-index; acceptable for the
    # rare revoke path (handshake reset). At most 2 sessions (op + cl).
    resp = _get_table().scan(
        FilterExpression=(
            Attr("pk").begins_with("SESSION#") &
            Attr("actor").eq(actor) &
            Attr("revoked_at").eq(None)
        )
    )
    now = _now()
    count = 0
    for item in resp.get("Items", []):
        _get_table().update_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            UpdateExpression="SET revoked_at = :t",
            ExpressionAttributeValues={":t": now},
        )
        count += 1
    return count


def _coerce_session(item: dict) -> dict:
    return {
        "bearer_hash":  item["bearer_hash"],
        "actor":        item["actor"],
        "created_at":   item["created_at"],
        "last_seen_at": item.get("last_seen_at"),
        "revoked_at":   item.get("revoked_at"),
    }


# pubkey

def put_pubkey(actor: str, kem_pub: bytes) -> None:
    _get_table().put_item(Item={
        "pk":           f"PUBKEY#{actor}",
        "sk":           f"PUBKEY#{actor}",
        "actor":        actor,
        "kem_pub":      bytes(kem_pub),
        "registered_at": _now(),
    })


def get_pubkey(actor: str) -> bytes | None:
    resp = _get_table().get_item(Key={
        "pk": f"PUBKEY#{actor}", "sk": f"PUBKEY#{actor}",
    })
    item = resp.get("Item")
    if not item:
        return None
    raw = item["kem_pub"]
    return bytes(raw)


def get_all_pubkeys() -> dict[str, bytes]:
    """Returns {actor: kem_pub_bytes} for all registered actors."""
    out: dict[str, bytes] = {}
    for actor in ("operator", "client"):
        pub = get_pubkey(actor)
        if pub is not None:
            out[actor] = pub
    return out


def list_pubkeys() -> list[dict]:
    """Returns [{actor, registered_at}] - no key material."""
    rows = []
    for actor in ("operator", "client"):
        resp = _get_table().get_item(Key={
            "pk": f"PUBKEY#{actor}", "sk": f"PUBKEY#{actor}",
        })
        item = resp.get("Item")
        if item:
            rows.append({"actor": item["actor"], "registered_at": item["registered_at"]})
    return rows


def delete_pubkey(actor: str) -> None:
    _get_table().delete_item(Key={"pk": f"PUBKEY#{actor}", "sk": f"PUBKEY#{actor}"})


# pack

class PackAlreadyExists(Exception):
    pass


def put_pack(
    *,
    pack_id: str,
    campaign_id: int,
    recipient_hash: str,
    content_hash: str,
    send_at: str,
    key_id: str,
    encrypted_payload: bytes,
    pushed_by: str,
) -> None:
    """Idempotent on pack_id - raises PackAlreadyExists on duplicate."""
    try:
        _get_table().put_item(
            Item={
                "pk":               f"PACK#{pack_id}",
                "sk":               f"PACK#{pack_id}",
                "pack_id":          pack_id,
                "campaign_id":      campaign_id,
                "recipient_hash":   recipient_hash,
                "content_hash":     content_hash,
                "send_at":          send_at,
                "key_id":           key_id,
                "encrypted_payload": bytes(encrypted_payload),
                "status":           "pending",
                "enqueued_at":      _now(),
                "pushed_by":        pushed_by,
                "claimed_at":       None,
                "sent_at":          None,
                "resend_message_id": None,
                "failure_reason":   None,
            },
            ConditionExpression=Attr("pack_id").not_exists(),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise PackAlreadyExists(pack_id)
        raise


def get_pack(pack_id: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": f"PACK#{pack_id}", "sk": f"PACK#{pack_id}"})
    item = resp.get("Item")
    return _coerce_pack(item) if item else None


def claim_pack(pack_id: str) -> bool:
    """Atomically flip status pending -> claimed. Returns True on success."""
    now = _now()
    try:
        _get_table().update_item(
            Key={"pk": f"PACK#{pack_id}", "sk": f"PACK#{pack_id}"},
            UpdateExpression="SET #s = :claimed, claimed_at = :now",
            ConditionExpression=Attr("status").eq("pending"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":claimed": "claimed", ":now": now},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def update_pack(pack_id: str, **fields) -> None:
    """Generic update for pack fields (status, sent_at, resend_message_id, etc.)."""
    if not fields:
        return
    set_expr = ", ".join(f"#{k} = :{k}" for k in fields)
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    _get_table().update_item(
        Key={"pk": f"PACK#{pack_id}", "sk": f"PACK#{pack_id}"},
        UpdateExpression=f"SET {set_expr}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def list_pending_packs(limit: int = 50) -> list[dict]:
    """Query pending packs ordered by send_at (ascending) via status-index GSI."""
    now_iso = _now()
    resp = _get_table().query(
        IndexName="status-index",
        KeyConditionExpression=(
            Key("status").eq("pending") & Key("send_at").lte(now_iso)
        ),
        Limit=limit,
        ScanIndexForward=True,
    )
    return [_coerce_pack(i) for i in resp.get("Items", [])]


def cancel_pending_for_campaign(campaign_id: int) -> int:
    """Cancel ALL still-pending packs for a campaign (any send_at), including seed
    packs that have no terminal-side dispatched_job row. Returns count cancelled.
    This is the correct way to abort a campaign - deleting only dispatched_job
    pack_ids misses salted seed packs and leaves them to fire."""
    table = _get_table()
    cancelled = 0
    kwargs = {
        "IndexName": "status-index",
        "KeyConditionExpression": Key("status").eq("pending"),
        "FilterExpression": Attr("campaign_id").eq(campaign_id),
    }
    while True:
        resp = table.query(**kwargs)
        for it in resp.get("Items", []):
            pid = it["pack_id"]
            table.update_item(
                Key={"pk": f"PACK#{pid}", "sk": f"PACK#{pid}"},
                UpdateExpression="SET #s = :c, encrypted_payload = :n",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":c": "cancelled", ":n": None},
            )
            cancelled += 1
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return cancelled


def list_resolved_packs(since_enqueued_at: str = "", limit: int = 500) -> list[dict]:
    """Return packs in terminal statuses (sent/failed/stuck_no_key/cancelled)
    with enqueued_at > since_enqueued_at, oldest-first.

    The status-index GSI sorts by send_at (which is '1970…' for immediate sends),
    so it can't range-key on enqueued_at. We filter enqueued_at via a
    FilterExpression and paginate forward so the cursor always advances - without
    this the terminal's outcomes loop freezes on the oldest page and never sees
    new packs. (Scale follow-up: add a GSI keyed on enqueued_at to avoid the
    full-partition read this filter implies.)"""
    resolved_statuses = {"sent", "failed", "stuck_no_key", "cancelled"}
    rows = []
    work_cap = max(limit * 4, 2000)
    for status in resolved_statuses:
        kwargs = {
            "IndexName": "status-index",
            "KeyConditionExpression": Key("status").eq(status),
            "ScanIndexForward": True,
        }
        if since_enqueued_at:
            kwargs["FilterExpression"] = Attr("enqueued_at").gt(since_enqueued_at)
        scanned = 0
        while True:
            resp = _get_table().query(**kwargs)
            rows.extend(_coerce_pack(i) for i in resp.get("Items", []))
            scanned += resp.get("ScannedCount", len(resp.get("Items", [])))
            lek = resp.get("LastEvaluatedKey")
            if not lek or scanned >= work_cap:
                break
            kwargs["ExclusiveStartKey"] = lek
    rows.sort(key=lambda r: r["enqueued_at"])
    return rows[:limit]


def _coerce_pack(item: dict) -> dict:
    payload = item.get("encrypted_payload")
    return {
        "pack_id":          item["pack_id"],
        "campaign_id":      int(item["campaign_id"]),
        "recipient_hash":   item["recipient_hash"],
        "content_hash":     item["content_hash"],
        "send_at":          item["send_at"],
        "key_id":           item["key_id"],
        "encrypted_payload": bytes(payload) if payload is not None else None,
        "status":           item["status"],
        "enqueued_at":      item.get("enqueued_at", ""),
        "pushed_by":        item.get("pushed_by", ""),
        "claimed_at":       item.get("claimed_at"),
        "sent_at":          item.get("sent_at"),
        "resend_message_id": item.get("resend_message_id"),
        "failure_reason":   item.get("failure_reason"),
    }


# webhook_event_raw

def put_webhook_raw(
    *,
    svix_id: str,
    event_type: str | None,
    message_id: str | None,
    forwarded_event_id: int | None = None,
) -> None:
    _get_table().put_item(Item={
        "pk":                  f"WEBHOOK#{svix_id}",
        "sk":                  f"WEBHOOK#{svix_id}",
        "svix_id":             svix_id,
        "event_type":          event_type or "",
        "message_id":          message_id or "",
        "received_at":         _now(),
        "forwarded_event_id":  forwarded_event_id,
    })


def get_webhook_by_svix(svix_id: str) -> dict | None:
    resp = _get_table().get_item(Key={
        "pk": f"WEBHOOK#{svix_id}", "sk": f"WEBHOOK#{svix_id}",
    })
    return resp.get("Item")


# settings

def get_setting(key: str) -> dict | None:
    resp = _get_table().get_item(Key={"pk": "SETTINGS", "sk": key})
    item = resp.get("Item")
    if not item:
        return None
    return {
        "key":        item["sk"],
        "value":      item.get("value", ""),
        "is_secret":  bool(item.get("is_secret", False)),
        "updated_at": item.get("updated_at", ""),
        "updated_by": item.get("updated_by", "system"),
    }


def put_setting(key: str, value: str, *, is_secret: bool, updated_by: str = "system") -> None:
    _get_table().put_item(Item={
        "pk":         "SETTINGS",
        "sk":         key,
        "value":      value,
        "is_secret":  is_secret,
        "updated_at": _now(),
        "updated_by": updated_by,
    })


def delete_setting(key: str) -> None:
    _get_table().delete_item(Key={"pk": "SETTINGS", "sk": key})


def list_settings() -> list[dict]:
    resp = _get_table().query(KeyConditionExpression=Key("pk").eq("SETTINGS"))
    return [
        {
            "key":        item["sk"],
            "value":      item.get("value", ""),
            "is_secret":  bool(item.get("is_secret", False)),
            "updated_at": item.get("updated_at", ""),
            "updated_by": item.get("updated_by", "system"),
        }
        for item in resp.get("Items", [])
    ]


# compliance cache

def put_unsub(email_hash: str, source_token: str, scope: str = "all") -> bool:
    """Store unsub. scope='promotional' or 'all'. Returns True if new, False if already present.

    Writes two items:
      - cache row  pk=UNSUB#{hash}      (point lookup / dedupe)
      - log row    pk=UNSUB_LOG sk={ts}#{hash}  (time-ordered, cheap Query for /sync/pull)
    """
    now = _now()
    table = _get_table()
    is_new = True
    try:
        table.put_item(
            Item={
                "pk":           f"UNSUB#{email_hash}",
                "sk":           f"UNSUB#{email_hash}",
                "email_hash":   email_hash,
                "source_token": source_token,
                "scope":        scope,
                "added_at":     now,
            },
            ConditionExpression=Attr("email_hash").not_exists(),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            is_new = False
            if scope == "all":
                table.update_item(
                    Key={"pk": f"UNSUB#{email_hash}", "sk": f"UNSUB#{email_hash}"},
                    UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "scope"},
                    ExpressionAttributeValues={":s": "all"},
                )
        else:
            raise

    # Append a time-ordered log entry on every write (new OR scope-upgrade) so the
    # bearer-authed pull catches it. Cheap Query partition, no table Scan.
    if is_new or scope == "all":
        table.put_item(Item={
            "pk":         "UNSUB_LOG",
            "sk":         f"{now}#{email_hash}",
            "email_hash": email_hash,
            "scope":      scope,
            "added_at":   now,
        })
    return is_new


def put_erasure(email_hash: str, source_token: str) -> None:
    ts = f"{int(time.time() * 1000):016d}"
    _get_table().put_item(Item={
        "pk":          "ERASURE",
        "sk":          f"{ts}#{email_hash[:8]}",
        "email_hash":  email_hash,
        "source_token": source_token,
        "added_at":    _now(),
    })


def list_unsubs(since_cursor: str = "", limit: int = 1000) -> list[dict]:
    """Return UNSUB_LOG entries with sk > since_cursor, oldest-first.

    Cheap Query on the UNSUB_LOG partition (same pattern as EVENT). The
    bearer-authed /sync/pull uses this so terminals catch self-service
    unsubscribes - the public /u/ endpoint never writes to the sync stream.
    Each returned row carries `cursor` (its sk) for precise resumption.
    """
    cond = Key("pk").eq("UNSUB_LOG")
    if since_cursor:
        cond = cond & Key("sk").gt(since_cursor)
    resp = _get_table().query(
        KeyConditionExpression=cond,
        Limit=limit,
        ScanIndexForward=True,
    )
    out = []
    for it in resp.get("Items", []):
        out.append({
            "email_hash": it["email_hash"],
            "scope":      it.get("scope", "all"),
            "added_at":   it.get("added_at", ""),
            "cursor":     it["sk"],
        })
    return out
