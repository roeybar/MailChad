#!/usr/bin/env python3
"""One-time backfill: create UNSUB_LOG entries for pre-existing UNSUB# cache rows.

Run once after deploying the v3.21 unsub-log change. Uses the operator's deploy
creds (which have dynamodb:Scan) - NOT the restricted Lambda role. Idempotent:
re-running only adds log rows that are missing.

  docker run --rm --env-file .env email-platform-v3-cloud python /deploy/backfill_unsub_log.py
"""
import os
import boto3
from boto3.dynamodb.conditions import Attr, Key

TABLE = os.environ["DYNAMODB_TABLE"]
region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
table = boto3.resource("dynamodb", region_name=region).Table(TABLE)

# 1. Collect existing log cursors so we don't duplicate.
existing = set()
resp = table.query(KeyConditionExpression=Key("pk").eq("UNSUB_LOG"))
while True:
    for it in resp.get("Items", []):
        existing.add(it["sk"])
    if "LastEvaluatedKey" not in resp:
        break
    resp = table.query(KeyConditionExpression=Key("pk").eq("UNSUB_LOG"),
                       ExclusiveStartKey=resp["LastEvaluatedKey"])

# 2. Scan UNSUB# cache rows, append missing log entries.
added = skipped = 0
kwargs = {"FilterExpression": Attr("pk").begins_with("UNSUB#")}
while True:
    resp = table.scan(**kwargs)
    for it in resp.get("Items", []):
        h = it["email_hash"]
        added_at = it.get("added_at", "1970-01-01T00:00:00Z")
        sk = f"{added_at}#{h}"
        if sk in existing:
            skipped += 1
            continue
        table.put_item(Item={
            "pk": "UNSUB_LOG", "sk": sk,
            "email_hash": h, "scope": it.get("scope", "all"), "added_at": added_at,
        })
        added += 1
    lek = resp.get("LastEvaluatedKey")
    if not lek:
        break
    kwargs["ExclusiveStartKey"] = lek

print(f"backfill done: {added} log rows added, {skipped} already present")
