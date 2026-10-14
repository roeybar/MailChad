# MailMan architecture

The package version is 4.0.0 (`src/mailman/__init__.py` and `pyproject.toml`).

---

## Section 1 - Topology

```
Operator terminal                 Client terminal
SQLite + own KEM private key      SQLite + own KEM private key
              |                   |
              +---- HTTPS --------+
                        |
                  ep-api Lambda
                  (FastAPI + Mangum)
                        |
                 DynamoDB + SQS
                        |
                 ep-dispatcher Lambda
                        |
                     Resend
```

The API handles sync, handshake, packs, webhooks, compliance and settings.
The dispatcher handles SQS records and an EventBridge due-pack sweep.
The terminal runs the admin UI, sync client, materialiser and scheduler.

`docker-compose.yml` runs only the terminal and publishes `8011:8000` without
restricting the host interface to localhost. The entrypoint binds to `0.0.0.0`.
WireGuard setup remains in the entrypoint but is skipped when its key variables
are empty, as they are in the production compose file.

`docker-compose.dev.yml` defines DynamoDB Local, localstack, cloud, dispatcher
and terminal services. See Known limitations in the README before using it.

---

## Section 2 - Encryption key model

### Section 2.1 - Key material

| Key | Where it lives | What it does |
|---|---|---|
| **Own KEM private key** | Each terminal's `TERMINAL_KEYS_DIR/own_kem_priv.key` | Decrypts the wrap addressed to that terminal. |
| **Peer KEM public key** | Each terminal's `TERMINAL_KEYS_DIR/peer_kem_pub.key` | Encrypts the peer's copy of the per-payload key. |
| **K_op_pub, K_cl_pub** | Registered on the cloud; each terminal has its own and its peer's public key | Wrap per-payload keys for both parties. |
| **K_temp** | Terminal cache and cloud DynamoDB | Decrypts packs while active; valid TTLs are 1h, 24h and 7d. |

`KeyBundle.load()` loads the terminal's own private key and the peer's public
key. See Known limitations in the README for the handshake helper.

### Section 2.2 - Two encryption modes

- **`K_op_pub + K_cl_pub` mode** - random per-payload AES-256-GCM key, wrapped
  twice with NaCl SealedBox. Either party decrypts its wrap with its own private
  key. Webhook events, and suppressions derived from them, are encrypted to both
  terminals' public keys; one-click unsubscribes are stored and synced as SHA-256
  email hashes.
- **`K_temp` mode** - symmetric AES-256-GCM with K_temp. The dispatcher decrypts packs with the active key.

### Section 2.3 - K_temp lifecycle

- **Mint:** the launch helper checks its cached key against `/key/temp/status`; if it cannot reuse it, it mints and posts a new key. The default TTL setting is 86400 seconds.
- **Storage:** `keys_dynamo.py` stores key bytes, expiry metadata and an active-key pointer in DynamoDB. Expired reads delete the active key and return no key.
- **Dispatch:** a pack with no active key becomes `stuck_no_key`; a decrypt failure becomes `failed`.
- **TTL setting:** `default_k_temp_ttl_s` is editable on the Overview page. There is no automatic stuck-pack re-encryption flow.

### Section 2.4 - Crypto stack

- **KEM:** X25519 via NaCl SealedBox (PyNaCl)
- **Symmetric:** AES-256-GCM via `cryptography`

---

## Section 3 - Sync protocol

### Section 3.1 - Sync push record

```json
{
  "table":             "webhook_event",
  "id":                "<row-id>",
  "revision":          47,
  "actor":             "operator",
  "modified_at":       "2026-05-19T01:23:45Z",
  "encrypted_payload": "<base64>",
  "key_id":            "K_op+K_cl",
  "deleted":           false
}
```

Cloud stores the blob plus plaintext metadata (`revision`, `actor`, `modified_at`, `table_name`, `row_id`, `key_id`, `deleted`). Cloud sorts/indexes on metadata; cannot read payloads it doesn't have the key for.

### Section 3.2 - Transport and cursors

```
GET /sync/pull?since=<integer>&unsub_since=<cursor>
  -> returns events, max_event_id, unsubs and max_unsub_at immediately

POST /sync/push
  body: [array of sync events]
  -> stores events and returns assigned_event_ids and near_conflicts
```

DynamoDB allocates integer event IDs with an atomic counter. The terminal stores
`last_pulled_event_id` in `sync_state` and polls every 5 seconds by default
(`SYNC_POLL_INTERVAL_S`). Pulled events enter `inbox`; duplicate
`(table_name, row_id, revision)` entries are skipped. The materialiser applies
`webhook_event` and `suppression` rows; it does not merge contact, template or
campaign edits between terminals.

### Section 3.3 - Conflict flagging

Cloud flags writes by different actors to the same `(table, row_id)` when their
`modified_at` timestamps differ by less than 60 seconds. The API exposes
`GET /sync/near-conflicts` and `POST /sync/near-conflicts/{conflict_id}/ack`.
It appends events rather than resolving them by a Lamport tie-breaker.

### Section 3.4 - Tombstones

Sync events carry a `deleted` flag. No cursor-based tombstone garbage collector
is implemented.

---

## Section 4 - Pack lifecycle (send execution)

### Section 4.1 - Launch flow

Launch validates the campaign and template, filters suppression and contact
locks, and builds rendered packs locally. Normal launch creates ordered batches
and dispatches batch 1; later batches use manual approval. Pack payloads include
the recipient, subject, HTML, text, headers and Resend credentials, encrypted
with K_temp. Contact jobs are recorded locally after the cloud acknowledges
pushes; retries reuse deterministic IDs for unrecorded contact sends.

### Section 4.2 - Cloud dispatcher

Due packs are enqueued immediately; future packs stay pending until the
EventBridge sweep enqueues them. The dispatcher conditionally claims a pending
pack, decrypts it with the active K_temp and calls Resend. It then records
`sent`, `failed` or `unknown` and clears the encrypted payload.

Network errors and Resend 5xx responses become `unknown` and stop that
invocation. These packs are not retried. Stale claims also become `unknown`.
Plaintext is used during the HTTP request.

### Section 4.3 - Webhook receipt (the special case)

```
POST /webhooks/resend                # public endpoint, HMAC verified
  1. svix HMAC verification with RESEND_WEBHOOK_SECRET
  2. Fail-closed if secret unset
  3. Parse event
  4. Encrypt event payload to BOTH K_op_pub AND K_cl_pub
     (using per-event ephemeral key + dual wrap, per Section 2.2)
  5. Write as a sync event (table='webhook_event', K_op+K_cl mode)
  6. Either terminal picks up on next sync
```

Webhooks survive past K_temp TTL because they're encrypted with the long-lived public keys, not K_temp.

---

## Section 5 - Public endpoints (cloud-side, internet-facing)

| Endpoint | Purpose | Auth |
|---|---|---|
| `POST /webhooks/resend` | Resend event ingestion | HMAC (svix) |
| `GET /u/<token>` | RFC 8058 unsub landing | HMAC token |
| `POST /u/<token>` | RFC 8058 one-click POST | HMAC token |
| `GET /e/<token>` | GDPR erasure landing | HMAC token |
| `POST /e/<token>` | GDPR erasure POST | HMAC token |
| `GET /healthz` | cloud status and counts | none |

Token HMAC verification reads `unsub_secret` and `erasure_secret` from cloud settings. The terminal's inline settings save handler attempts to push these values to the cloud; they are not delivered by handshake or encrypted with K_temp.

---

## Section 6 - Sync API (cloud-side, terminal-facing, HTTPS + auth)

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /sync/pull?since=N` | short-poll for new events | bearer (per-terminal) |
| `POST /sync/push` | append terminal events | bearer |
| `POST /packs` | upload encrypted send-jobs | bearer |
| `GET /packs/status?since_id=<cursor>` | pull dispatch results | bearer |
| `POST /key/temp` | provision or replace K_temp | bearer |
| `DELETE /key/temp` | revoke K_temp | bearer |
| `POST /init/handshake` | register a role's public key | bootstrap token |

**Bearer auth:** handshake generates a random token with `secrets.token_urlsafe(48)` and stores its SHA-256 hash in DynamoDB. Revoking `/init/handshake/{role}` with the bootstrap token deletes that public key and revokes its sessions. K_temp refresh does not rotate bearer tokens.

---

## Section 7 - Init handshake script

`scripts/_init_handshake.py` generates or reuses the local X25519 keypair,
registers the public key using `X-Bootstrap-Token`, and saves the issued bearer.
It prints the local public key, prompts for the peer's public key and compares
that key with `/pubkeys` when the peer is registered.

Each terminal's `KeyBundle.load()` loads its own private key and the peer's
public key. See Known limitations in the README for the handshake helper.

---

## Section 8 - Sender configuration

Sender configuration is stored in terminal `entities` rows. Launch uses the
campaign's entity credentials, or global sender settings when there is no
entity. Each pack carries its Resend key. There is no `domain` table or
per-campaign domain rotation strategy in the current schema.

Quick send and campaign Test send post one message from the terminal straight
to Resend with the sender key, outside the cloud relay and its encryption.

---

## Section 9 - Backup helper

`scripts/_backup.py` snapshots local SQLite and the key directory, optionally
scans DynamoDB, and records SQS queue counts. It encrypts snapshots with
AES-256-GCM using a passphrase-derived scrypt key and writes
`v3-backup-<actor>-<timestamp>.zip` with an integrity manifest.

See Known limitations in the README before using the backup wrapper.

---

## Section 10 - Tests and probes

Run the default suite inside the project's test container:

```bash
bash scripts/v3 test
```

The October 2026 baseline is 650 passed, 1 skipped. AWS and Resend are mocked;
the real-send path has not been run against Resend for this release.

- `tests/test_sync_protocol.py` covers push/pull, auth and near-conflicts.
- `tests/test_keys_lifecycle.py` covers K_temp TTL and key endpoints.
- `tests/test_encryption.py` covers round-trips and tamper detection.
- `tests/test_dispatcher.py` covers mocked dispatcher sends.
- `tests/test_webhook.py` covers signature verification and encrypted events.
- `tests/test_pack_push_retry.py` includes long seeded send-path walks.
- `tests/test_archive_send.py` includes archive/cancel walks.
- `tests/live_e2e.py` is a separate attended demo script; see `docs/LIVE-E2E.md`.

Probe scripts exist under `scripts/_probe/`; see Known limitations in the README
for the wrapper's `probe` verb.

---

## Section 11 - Trade-offs

- **Cloud holds K_temp during TTL** - compromise during that period exposes pack payloads decryptable with the active key.
- **Own private keys** - each terminal decrypts its addressed wrap; see Known limitations in the README for the handshake helper.
- **Sync scope** - incoming webhook and suppression events are materialised; general cross-terminal contact/template/campaign merging is not implemented.
- **Deployment scope** - the default configuration uses one DynamoDB table; deployment helpers use fixed Lambda names.

---

v3.2 moved the cloud side to Lambda and DynamoDB so that each party runs only a terminal.


## Section 12 - Settings model

Terminal settings use the SQLite `settings` table. Cloud settings use DynamoDB
items with `pk='SETTINGS'` and `sk=<key>`. Both store the value, secret flag,
update time and updater.

### Encryption at rest

Secrets (`SECRET_KEYS` constant per side) are encrypted with AES-256-GCM
using a per-side KEK:

- Cloud: `CLOUD_KEYS_DIR/settings_kek.bin` (0600)
- Terminal: `TERMINAL_KEYS_DIR/settings_kek.bin` (0600)

The KEK is loaded or generated when settings encryption is first used and is
also initialised by `migrate_from_env()`. It contains 32 random bytes. Losing it
prevents decryption of existing secret settings.

Secrets decrypt on each `settings.get()` call. Cloud non-secrets are cached;
terminal `get()` queries SQLite, while `get_all()` uses a non-secret cache.

### Secret keys

- **Cloud:** `resend_webhook_secret`, `unsub_secret`, `erasure_secret`
- **Terminal:** `admin_password_hash`, `jwt_secret`, `unsub_secret`, `erasure_secret`, `resend_api_key`

The settings copy of `admin_password_hash` is bcrypt'd and encrypted. Login
also reads bcrypt hashes directly from the `operators` table.

### env-only (by necessity)

- `BOOTSTRAP_TOKEN` (cloud) - chicken-egg with handshake auth
- `CLOUD_URL`, `CLOUD_BEARER`, `TERMINAL_ACTOR` (terminal) - cloud location, bearer and role
- `VAULT_DB_PATH`, `CLOUD_KEYS_DIR`, `TERMINAL_KEYS_DIR` - filesystem locations
- `LOG_LEVEL` - logging level
- `AWS_REGION`, `AWS_DEFAULT_REGION`, `DYNAMODB_TABLE`, `DYNAMODB_ENDPOINT`, `SQS_QUEUE_URL`, `SQS_ENDPOINT` - cloud infrastructure

### Boot-time migration

The terminal startup calls `db.init()`, migrates legacy admin credentials into
`operators` when that table is empty, then calls `settings.migrate_from_env()`.
Cloud startup calls `dynamo.init()` and `settings.migrate_from_env()`, but the
Lambda Mangum handler has `lifespan='off'`, so it does not run that startup hook.
Existing settings rows are not overwritten by env migration.

### Admin UI panels

- `/admin` - K_temp TTL
- `/admin/entities` - brand, global sender, Resend API key and cloud settings forms
- `/admin/operators` - session lifetime, password change and JWT rotation
- `/admin/suppression` - unsub/erasure secrets and token TTLs
- `/admin/settings/sending` - send window, pacing and batch size
- `send_daily_cap` - read from settings; configurable through `/admin/settings/api`

Old `/admin/settings/*` pages redirect to these feature pages.
`/admin/settings/reveal/{key}` reveals a terminal secret and logs the access.

### REST API

Both sides expose CRUD endpoints:

- Cloud: `/settings` and its key/reveal routes - bearer auth (the terminal reads these for the cloud forms)
- Terminal: `/admin/settings/api/*` - session-cookie auth

Both expose list (secrets redacted), single-read, reveal (logged), upsert and delete.

### Cross-side secret sync

The inline terminal save handler attempts to POST `unsub_secret` and
`erasure_secret` to cloud `/settings` using the stored bearer. A failure leaves
the local value saved and is logged. Generic `/admin/settings/api` writes do
not invoke this push. There is no cloud-to-terminal settings auto-pull.

### Tests

`tests/test_settings.py` has 13 test functions covering KEK lifecycle,
encryption at rest, tamper detection, migration idempotency and env fallback.
