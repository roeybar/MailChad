# v3.2 - Lambda + DynamoDB cloud spec

**Goal:** replace the always-on cloud Docker container with two independent AWS Lambda
functions + DynamoDB. The send queue and the send executor warm/cool independently.
Everything else (terminals, crypto, sync protocol shape) unchanged.

---

## Architecture

```
Terminal-op (laptop)   ──┐
                          │  HTTPS via API Gateway
Terminal-cl (PC)       ──┤──────────────────────────->  ep-api Lambda
                          │                                   │
Resend webhooks  ─────────┘                            DynamoDB table
                                                             │
                          pack_ids -> SQS ───────────────────┘
                                      │
                               ep-dispatcher Lambda
                                      │
                               Resend API  ->  email sent
```

- **ep-api Lambda** - FastAPI (Mangum). Handles all HTTP: sync, handshake, packs push,
  webhook ingest, compliance, settings. Stateless; spins per request.
- **ep-dispatcher Lambda** - SQS consumer. One job: decrypt K_temp -> POST Resend -> record result.
  Warms when queue has messages, cools when empty. Never touches the HTTP path.
- **DynamoDB** - all persistent state. TTL attribute on K_temp rows for auto-wipe.
- **SQS** - send queue. API Lambda enqueues pack_id on every pack push. Dispatcher drains it.
- **Local dev** - `amazon/dynamodb-local` container replaces SQLite; same boto3 code paths.

No VPS. No always-on container. No long-poll sync (replaced by request/response pull).

---

## New env vars (add to .env + .env.example)

```
AWS_ACCESS_KEY_ID=PLACEHOLDER
AWS_SECRET_ACCESS_KEY=PLACEHOLDER
AWS_REGION=us-east-1
DYNAMODB_TABLE=ep-v3-prod
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT_ID/ep-send-queue
# Local dev only - point boto3 at the local container:
DYNAMODB_ENDPOINT=http://dynamodb-local:8000
SQS_ENDPOINT=http://localstack:4566
```

---

## DynamoDB table design

Single table `ep-v3-{engagement}` (on-demand billing - no capacity planning).

| Entity | PK | SK | Notes |
|---|---|---|---|
| event_log | `EVENT` | `{ulid}` | ULID = time-ordered; cursor = last SK seen |
| pack | `PACK#{pack_id}` | `PACK#{pack_id}` | status, encrypted_payload, K_temp key_id |
| near_conflict | `CONFLICT` | `{detected_at}#{ulid}` | flagged at push time |
| terminal_session | `SESSION#{bearer_hash}` | `SESSION#{bearer_hash}` | actor, revoked_at |
| pubkey | `PUBKEY#{actor}` | `PUBKEY#{actor}` | kem_pub (Binary) |
| webhook_event | `WEBHOOK#{svix_id}` | `WEBHOOK#{svix_id}` | dedup on svix_id |
| settings | `SETTINGS#{side}` | `{key}` | value, is_secret, encrypted blob |
| ktemp | `KTEMP#{key_id}` | `KTEMP#{key_id}` | key_b64, `ttl` attr (unix epoch -> DynamoDB auto-expire) |
| counter | `COUNTER` | `{name}` | atomic ADD for any sequence needed |

**GSI-1** (for pack status queries, backup path if SQS loses a message):
- PK = `status`, SK = `send_at` -> query `status=pending ORDER BY send_at`

All items have `entity_type` attribute for filter expressions.

### Sync cursor migration

Old: `event_id INTEGER AUTOINCREMENT` + `WHERE event_id > ?`  
New: `SK = ULID` (lexicographically sortable) + `SK > last_ulid`

Terminal stores `last_event_ulid` instead of `last_event_id`. Pull endpoint:

```
GET /sync/pull?cursor=01HX...  ->  {events: [...], next_cursor: "01HY..."}
```

No timeout. No hold. Returns immediately with whatever is newer than cursor.
Terminal polls on open + after any local write. Sleep interval between polls: 5s (configurable).

---

## ep-api Lambda

**Handler:** `cloud/lambda_handler.py`  
```python
from mangum import Mangum
from app.main import app
handler = Mangum(app, lifespan="off")
```

**Startup changes:**
- Remove `dispatcher_loop` asyncio task (SQS owns that now)
- Replace `db.init()` / SQLite calls -> `dynamo.init()` (create table if not exists, idempotent)
- `settings.migrate_from_env()` still runs on cold start

**Route changes:**

| Old | New |
|---|---|
| `GET /sync` (long-poll, holds connection) | `GET /sync/pull?cursor=ULID` (returns immediately) |
| `POST /sync/push` (writes to SQLite) | `POST /sync/push` (writes to DynamoDB) |
| `POST /packs` (writes to SQLite, dispatcher loop picks up) | `POST /packs` (writes to DynamoDB + enqueues pack_id to SQS) |
| All other routes | Unchanged shape; storage calls -> DynamoDB |

**Auth:** same bearer -> SHA-256 -> DynamoDB lookup. No change in contract.

**Package size target:** <50MB zipped (FastAPI + Mangum + boto3 + PyNaCl + cryptography).
Use Lambda layer for boto3 (already in runtime) and strip test deps.

---

## ep-dispatcher Lambda

**Handler:** `cloud/dispatcher_lambda.py`

```python
import json, boto3
from app.dynamo import get_pack, mark_sent, mark_failed
from app.encryption_cloud import decrypt_ktemp
from app.keys_dynamo import get_ktemp

def handler(event, context):
    for record in event["Records"]:
        pack_id = json.loads(record["body"])["pack_id"]
        _process(pack_id)

def _process(pack_id):
    pack = get_pack(pack_id)
    if pack["status"] != "pending":
        return  # idempotent - already processed
    key = get_ktemp(pack["key_id"])
    if not key:
        mark_failed(pack_id, "stuck_no_key")
        return
    plaintext = decrypt_ktemp(key, pack["encrypted_payload"])
    # POST to Resend
    ...
    mark_sent(pack_id, resend_message_id)
    # Write sync event so terminals pick it up
    write_event(table_name="pack", row_id=pack_id, ...)
```

**SQS trigger config:**
- Batch size: 10
- Visibility timeout: 60s (generous; typical Resend POST < 2s)
- Max receive count: 3 -> DLQ on failure
- DLQ retention: 7 days

**Concurrency:** unreserved (scales with queue depth automatically).

---

## K_temp lifecycle changes

Old: filesystem at `cloud/keys/{key_id}.json` + background wipe loop  
New: DynamoDB item `PK=KTEMP#{key_id}` with `ttl` attribute (unix epoch)

DynamoDB TTL auto-deletes the item within ~48h of expiry (usually <5 min).
`keys_dynamo.py` wraps get/set/wipe. `get_ktemp()` returns `None` if item absent or expired.

---

## Local dev - docker-compose additions

```yaml
services:
  dynamodb-local:
    image: amazon/dynamodb-local:latest
    command: ["-jar", "DynamoDBLocal.jar", "-sharedDb", "-inMemory"]
    ports:
      - "127.0.0.1:8001:8000"

  localstack:
    image: localstack/localstack:latest
    environment:
      SERVICES: sqs
      DEFAULT_REGION: us-east-1
    ports:
      - "127.0.0.1:4566:4566"

  ep-v3-cloud:
    # unchanged image; add env:
    environment:
      DYNAMODB_ENDPOINT: http://dynamodb-local:8000
      SQS_ENDPOINT: http://localstack:4566
      DYNAMODB_TABLE: ep-v3-dev
      SQS_QUEUE_URL: http://localstack:4566/000000000000/ep-send-queue
      DISABLE_DISPATCHER: "1"  # dispatcher runs as separate container in dev

  ep-v3-dispatcher:
    build: .
    command: ["python", "-m", "cloud.dispatcher_local"]  # local SQS poller wrapper
    environment:
      DYNAMODB_ENDPOINT: http://dynamodb-local:8000
      SQS_ENDPOINT: http://localstack:4566
      ...
```

`dispatcher_local.py` - thin wrapper that polls SQS in a loop and calls the same
`dispatcher_lambda.handler` function. Identical code path, no Lambda runtime needed locally.

---

## scripts/v3 new verbs

```
scripts/v3 setup-tables           Create DynamoDB table + SQS queue (local or prod)
scripts/v3 deploy-api             Package + deploy ep-api Lambda to AWS
scripts/v3 deploy-dispatcher      Package + deploy ep-dispatcher Lambda to AWS
scripts/v3 tail-api               CloudWatch log tail for ep-api
scripts/v3 tail-dispatcher        CloudWatch log tail for ep-dispatcher
scripts/v3 stress-queue [--count N]   Push N dummy packs to SQS, time the drain
scripts/v3 stress-sync  [--count N]   Push N events, verify cursor advances correctly
scripts/v3 stress-webhook [--count N] Fire N webhook payloads, verify event_log
```

---

## Tests

**Unit (moto mocks - run inside project container, no real AWS):**

```
tests/test_dynamo_events.py     push/pull cursor, near-conflict, dedup
tests/test_dynamo_packs.py      push, claim, idempotency, stuck_no_key
tests/test_dynamo_ktemp.py      set/get/expire/wipe, TTL attribute
tests/test_dynamo_sessions.py   register, bearer lookup, revoke
tests/test_dispatcher.py        SQS record -> decrypt -> mock Resend -> mark_sent
tests/test_sync_pull.py         GET /sync/pull cursor contract
```

**Adversarial probes (existing, adapted for DynamoDB):**

All existing `scripts/v3 probe *` verbs continue to work - they hit the HTTP surface,
not the storage layer. No changes needed to the probe scripts.

**Stress:**

`bin/_stress/stress_send_queue.py` - pushes N packs (default 500) with fake K_temp keys
to SQS, measures wall-clock drain time. Verifies DynamoDB `status=sent` count matches N.
Asserts no duplicates (idempotency check on `resend_message_id`).

---

## IAM permissions (minimum viable)

**ep-api execution role:**
```json
{
  "Action": [
    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
    "dynamodb:Query", "dynamodb:DeleteItem",
    "sqs:SendMessage"
  ],
  "Resource": ["arn:aws:dynamodb:*:*:table/ep-v3-*",
               "arn:aws:sqs:*:*:ep-send-queue"]
}
```

**ep-dispatcher execution role:**
```json
{
  "Action": [
    "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem",
    "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"
  ],
  "Resource": ["arn:aws:dynamodb:*:*:table/ep-v3-*",
               "arn:aws:sqs:*:*:ep-send-queue",
               "arn:aws:sqs:*:*:ep-send-dlq"]
}
```

---

## Free tier headroom (low-volume campaign client)

| Resource | Free tier | Expected usage |
|---|---|---|
| Lambda invocations | 1M/mo always | <10K/mo |
| Lambda compute | 400K GB-s always | <5K GB-s |
| DynamoDB reads | 25 RCU always | <1 RCU avg |
| DynamoDB writes | 25 WCU always | <1 WCU avg |
| SQS messages | 1M/mo always | <50K/mo |
| API Gateway HTTP | 1M/mo (12mo free) | <10K/mo |
| CloudWatch logs | 5GB/mo free | minimal |

**Cost after 12 months:** ~$0 at low volume. API Gateway HTTP costs $1/M requests;
at 10K/mo that's $0.01/mo.

---

## Migration plan (phases)

**Phase 1 - storage layer**
- `cloud/app/dynamo.py` - DynamoDB client + all CRUD wrappers (replaces `db.py`)
- `cloud/app/keys_dynamo.py` - K_temp via DynamoDB TTL (replaces `keys.py` filesystem)
- Unit tests with moto
- docker-compose gets `dynamodb-local` + `localstack`
- Cloud container runs against local DynamoDB in dev

**Phase 2 - sync redesign**
- `cloud/app/sync.py` - remove long-poll, add `/sync/pull?cursor=ULID`
- `terminal/app/sync_client.py` - switch from long-poll loop to short-poll (5s) + cursor
- Tests: cursor contract, dedup, near-conflict

**Phase 3 - dispatcher split**
- `cloud/dispatcher_lambda.py` - standalone SQS handler
- `cloud/dispatcher_local.py` - local SQS poller (same handler, loop wrapper)
- `cloud/app/packs.py` - remove dispatcher_loop, add SQS enqueue on pack push
- docker-compose gets `ep-v3-dispatcher` service
- Tests + stress

**Phase 4 - Lambda packaging**
- `cloud/lambda_handler.py` - Mangum entry point
- `scripts/v3 deploy-api` + `scripts/v3 deploy-dispatcher` scripts
- IAM roles (as JSON in `deploy/iam/`)
- API Gateway HTTP API config (as `deploy/api-gateway.tf` or CLI script)
- `scripts/v3 setup-tables` for prod table creation

**Phase 5 - terminal adaptation**
- Update `scripts/v3 init-handshake` to point at API Gateway URL instead of localhost
- `docs/CLIENT_HOSTED_DEPLOY.md` - replace "Cloudflare Tunnel + Docker cloud" with "API Gateway URL (no infra needed)"
- `README.md` update

---

## Open questions

1. Engagement isolation: one DynamoDB table per engagement (current spec) vs one table total
   with `engagement_id` prefix in PK? Single table is simpler; multi-engagement would need prefix.
2. Lambda deployment tooling: raw AWS CLI scripts in `scripts/v3` vs minimal Terraform vs SAM?
   CLI scripts keep zero new tools; SAM is cleaner but adds a dependency.
3. ULID library: `python-ulid` (pure Python) or `ulid-py`? Both tiny.
4. Cold-start budget: Mangum + FastAPI cold start is ~800ms. Acceptable for sync/webhook
   (non-interactive). If handshake UX suffers, add provisioned concurrency (minimal cost).
