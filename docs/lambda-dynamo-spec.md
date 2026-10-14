# Lambda + DynamoDB cloud implementation

The cloud has an HTTP API Lambda and a dispatcher Lambda backed by DynamoDB
and SQS. The local terminal retains its SQLite database and encryption keys.

---

## Architecture

```
Terminal-op (laptop)   --+
                          |  HTTPS via API Gateway
Terminal-cl (PC)       --+--------------------------->  ep-api Lambda
                          |                                   |
Resend webhooks  ---------+                            DynamoDB table
                                                             |
                          pack_ids -> SQS -------------------+
                                      |
                               ep-dispatcher Lambda
                                      |
                               Resend API  ->  email sent
```

- **ep-api Lambda** - FastAPI (Mangum). Handles all HTTP: sync, handshake, packs push,
  webhook ingest, compliance and settings.
- **ep-dispatcher Lambda** - SQS consumer and EventBridge due-pack sweep. Decrypts
  packs with the active K_temp, POSTs to Resend and records the result.
- **DynamoDB** - persistent cloud state, including K_temp key bytes and expiry metadata.
- **SQS** - due-pack queue. Future packs are enqueued by the dispatcher sweep.
- **Local dev** - DynamoDB Local and localstack SQS are defined in `docker-compose.dev.yml`.

No VPS. No always-on container. No long-poll sync (replaced by request/response pull).

---

## Infrastructure environment variables

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

Single table named by `DYNAMODB_TABLE` (default `ep-v3-dev`), with
`PAY_PER_REQUEST` billing.

| Entity | PK | SK | Notes |
|---|---|---|---|
| event_log | `EVENT` | `{event_id:010d}` | Integer IDs allocated by an atomic counter. |
| pack | `PACK#{pack_id}` | `PACK#{pack_id}` | status, encrypted_payload, K_temp key_id |
| near_conflict | `CONFLICT` | `{conflict_id:010d}` | Integer conflict IDs; flagged at push time. |
| terminal_session | `SESSION#{bearer_hash}` | `SESSION#{bearer_hash}` | actor, revoked_at |
| pubkey | `PUBKEY#{actor}` | `PUBKEY#{actor}` | kem_pub (Binary) |
| webhook_event | `WEBHOOK#{svix_id}` | `WEBHOOK#{svix_id}` | dedup on svix_id |
| settings | `SETTINGS` | `{key}` | value, is_secret, encrypted blob |
| ktemp | `KTEMP#{key_id}` | `KTEMP#{key_id}` | Binary `key_bytes`, `expires_at`, `ttl`; `KTEMP_ACTIVE` points to the active key. |
| counter | `COUNTER` | `{name}` | atomic ADD for any sequence needed |

**Secondary indexes:**

- `row-key-index`: PK = `row_key`, SK = `sk`, used for prior writes to a row.
- `status-index`: PK = `status`, SK = `send_at`, used for due pending packs.

Compliance also uses `UNSUB#<hash>`, `UNSUB_LOG` and `ERASURE` items.
There is no common `entity_type` attribute.

### Sync cursor

```
GET /sync/pull?since=123
  -> {events: [...], max_event_id: 124, unsubs: [...], max_unsub_at: "..."}
```

The terminal stores `last_pulled_event_id` and `last_pulled_unsub_at` in
`sync_state`. Pull returns immediately. The default poll interval is 5 seconds
(`SYNC_POLL_INTERVAL_S`).

---

## ep-api Lambda

**Handler:** `src/mailman/cloud/lambda_handler.py`
```python
from mangum import Mangum
from mailman.cloud.main import app
handler = Mangum(app, lifespan="off")
```

Cloud `main.py` has a startup hook for `dynamo.init()` and settings env migration,
but Mangum disables lifespan events. The Lambda handler does not call that hook.
Dispatch runs separately from the HTTP process.

| Route | Behavior |
|---|---|
| `GET /sync/pull?since=N` | Returns integer-cursor events and unsubscribe rows immediately. |
| `POST /sync/push` | Appends events to DynamoDB and flags near-conflicts. |
| `POST /packs` | Stores packs; enqueues due packs to SQS, defers future packs. |
| `GET /packs/status?since_id=<cursor>` | Returns resolved dispatch outcomes. |

**Auth:** sync, packs, K_temp and settings endpoints validate a bearer token by
SHA-256 hash lookup in DynamoDB. Handshake requires `X-Bootstrap-Token`.

---

## ep-dispatcher Lambda

**Handler:** `src/mailman/cloud/dispatcher_lambda.py`

SQS records carry `{"pack_id": "<id>"}`. The handler skips non-pending packs,
conditionally claims pending ones, gets the active K_temp and decrypts the pack.
No active key produces `stuck_no_key`; a decrypt failure produces `failed`.
The Resend call produces `sent`, `failed` or `unknown`, and finalisation clears
the encrypted payload. Network errors and 5xx responses are `unknown`, never
retried; the invocation stops so other pending records can be redelivered.
A sweep also changes stale claims to `unknown`.

**SQS setup:** `deploy/setup_tables.py` configures a 60-second visibility
timeout, max receive count 3, one-day main queue retention and seven-day DLQ
retention. `deploy/deploy_lambda.py` creates the dispatcher mapping with batch
size 10 on first creation and asserts an EventBridge sweep, default
`rate(1 minute)` (`SWEEP_RATE`).

---

## K_temp lifecycle

`src/mailman/cloud/keys_dynamo.py` stores binary key bytes and expiry metadata
under `KTEMP#{key_id}` plus a `KTEMP_ACTIVE` pointer. `get_active_k_temp()`
returns no key if the pointer/key is missing; an expired read wipes the active
key. Automatic expiry through the `ttl` attribute requires table TTL to be
enabled. `deploy/setup_tables.py` does not enable it, and the Lambda startup
hook that would enable it is off. Expired keys are refused on read through the
`expires_at` check without relying on deletion timing.

---

## Local development

`docker-compose.dev.yml` defines `dynamodb-local` (`amazon/dynamodb-local:latest`),
`localstack` (`localstack/localstack:3`), `cloud`, `ep-v3-dispatcher` and `terminal`.
DynamoDB Local publishes `127.0.0.1:8001:8000`; localstack publishes
`127.0.0.1:4566:4566`. See Known limitations in the README before using the dev stack.

`src/mailman/cloud/dispatcher_local.py` polls SQS and passes reshaped records
to the dispatcher handler. See Known limitations in the README before using
the local dispatcher.

---

## scripts/v3 cloud verbs

```
scripts/v3 setup-tables           Create DynamoDB table + SQS queue (local or prod) (see Known limitations)
scripts/v3 deploy-api             API deployment helper (see Known limitations in the README)
scripts/v3 deploy-dispatcher      Dispatcher deployment helper (see Known limitations in the README)
scripts/v3 tail-api               CloudWatch log tail for ep-api
scripts/v3 tail-dispatcher        CloudWatch log tail for ep-dispatcher
scripts/v3 wire-sqs-trigger      Create a missing dispatcher SQS mapping (see Known limitations)
```

---

## Tests

**Default suite:**

```bash
bash scripts/v3 test
```

The October 2026 baseline is 650 passed, 1 skipped. AWS and Resend are mocked.
`tests/test_dynamo_ops.py`, `tests/test_packs.py`, `tests/test_keys_lifecycle.py`,
`tests/test_handshake.py`, `tests/test_dispatcher.py` and
`tests/test_sync_protocol.py` cover the storage and API paths.
`tests/test_pack_push_retry.py` and `tests/test_archive_send.py` include seeded
stress walks. The real-send path has not been run against Resend for this release.

Probe files are under `scripts/_probe/`; see Known limitations in the README
for the wrapper's `probe` verb.

---

## Deployment helpers and IAM

`deploy/iam/ep-api-policy.json` allows DynamoDB GetItem, PutItem, UpdateItem,
DeleteItem and Query on the table and its indexes, SQS SendMessage and logging.
`deploy/iam/ep-dispatcher-policy.json` allows DynamoDB GetItem, PutItem,
UpdateItem and Query, SQS receive/delete/attributes/send operations and logging.

`deploy/deploy_lambda.py` uses boto3 and fixed function names `ep-api` and
`ep-dispatcher` and Python 3.12. On creation, timeouts are 30 s for ep-api,
60 s for ep-dispatcher, with 256 MB memory for each.
First creation requires `LAMBDA_ROLE_ARN` or `LAMBDA_DISPATCHER_ROLE_ARN`.
The configured Lambda environment contains `DYNAMODB_TABLE` and `SQS_QUEUE_URL`.
The API helper prints a next step to configure API Gateway; it does not create
an API Gateway or print its URL. Bootstrap and webhook secrets need separate
configuration. The dispatcher helper creates its SQS mapping on first creation
and asserts the sweep schedule on each deployment.

See Known limitations in the README before using the wrapper's deployment helpers.
