# Email Platform v3.1 - Cloud-Sync Spinnable Architecture

**Supersedes:** `email-platform-v3.0-architecture.md` (which described the WG-mediated "spinnable vault" version). v3.1 keeps the spinnable + cache-only-the-irreplaceable spirit but pivots topology: cloud-mediated sync between two local terminals, no WireGuard, no wake-hook.

**Status:** Design locked 2026-05-19. Implementation to follow. This is the contract - every commit on the v3 codebase should be traceable to a section here.

---

## §1 - Topology (load-bearing)

```
   OPERATOR LAPTOP                              CLIENT LAPTOP
   (docker compose)                             (docker compose)
   ┌──────────────────────────┐                ┌──────────────────────────┐
   │ Local terminal           │                │ Local terminal           │
   │ - localhost admin UI     │                │ - localhost admin UI     │
   │ - SQLite full-cache      │                │ - SQLite full-cache      │
   │ - encrypted at rest      │                │ - encrypted at rest      │
   │ - holds K_op_priv        │                │ - holds K_cl_priv        │
   │ - holds K_cl_priv (copy) │                │ - holds K_op_priv (copy) │
   │ - holds K_op_pub, K_cl_pub                │ - same                   │
   │ - one-click full backup  │                │ - one-click full backup  │
   │ - works offline          │                │ - works offline          │
   └────────┬─────────────────┘                └──────────┬───────────────┘
            │                                              │
            │   HTTPS sync (long-poll, encrypted payloads)│
            └──────────────┬───────────────────────────────┘
                           ▼
                ┌──────────────────────────────────────┐
                │ CLOUD SERVER (operator-hosted)       │
                │ - public HTTPS                       │
                │ - dumb storage + execution loop      │
                │ - holds K_op_pub, K_cl_pub           │
                │ - holds K_temp with TTL 1h/24h/7d    │
                │ - encrypted-at-rest blobs (cannot   │
                │   decrypt K_op+K_cl payloads)        │
                │ - can decrypt K_temp payloads        │
                │   only during TTL                    │
                │ - public endpoints: /webhooks/resend,│
                │   /u/<token>, /e/<token>             │
                │ - sync API: /sync GET (long-poll),   │
                │   /sync/push POST                    │
                │ - pack API: /packs POST + dispatcher │
                │ - 3 Resend API keys (one per sender  │
                │   domain), encrypted with K_temp     │
                └──────────────────────────────────────┘
                           ▲                    ▼
              Resend webhooks in        Resend POSTs out
              (HMAC verified;           (per-domain key;
              encrypted to              microsecond plaintext
              K_op_pub + K_cl_pub)      window)
```

**Component count:** 1 cloud server + 2 local terminals = 3 deployed pieces. WireGuard is GONE. Wake-hook is GONE. Backup module is folded INTO each local terminal.

---

## §2 - Encryption key model (the heart of the design)

### §2.1 - Three key materials

| Key | Where it lives | What it does |
|---|---|---|
| **K_op_priv** | Operator's terminal (primary) + Client's terminal (recovery copy) | Decrypts data encrypted to K_op_pub. Signs as operator. |
| **K_cl_priv** | Client's terminal (primary) + Operator's terminal (recovery copy) | Decrypts data encrypted to K_cl_pub. Signs as client. |
| **K_op_pub, K_cl_pub** | Everywhere (operator + client + cloud) | Encrypt-to. Verify signatures. Public, can be on stickers. |
| **K_temp** | Cloud only; TTL 1h / 24h / 7d, operator picks per launch | Decrypts packs at dispatch time. Wiped on TTL expiry. |

**Recovery model (Reading A - chosen):** each terminal holds BOTH private keys. Trade-off acknowledged: compromise of one PC = full breach (attacker gets both keys at once). Mitigation: the 6-location backup property below makes data-loss require simultaneous compromise of 6 independent stores, but data-disclosure on one-PC-compromise is just accepted.

### §2.2 - Two encryption modes

- **`K_op_pub + K_cl_pub` mode** - random per-row ephemeral key encrypts payload; ephemeral key wrapped TWICE (once each pubkey); both wraps travel with the row. Either party can independently decrypt locally. **Cloud CANNOT decrypt.** Used for: contacts, templates, campaigns, suppression, audit log, history, webhook events.
- **`K_temp` mode** - symmetric AES-256-GCM with K_temp. **Cloud CAN decrypt during TTL.** Used for: packs only.

### §2.3 - K_temp lifecycle

- **First mint (automagic):** first terminal to connect to a fresh cloud detects no active K_temp -> mints one with default 24h TTL -> ships to cloud over an authenticated POST. No human button.
- **Refresh ("auto sync to current campaign" button):** visible on any campaign-detail page when stuck packs are present. Click triggers: mint new K_temp -> push to cloud -> fetch stuck packs (encrypted with old K_temp from before expiry, undecryptable) -> terminal pulls the source data from its local SQLite -> re-encrypts packs with new K_temp -> pushes back. Either party can click; first-wins.
- **Expiry:** cloud-side TTL watchdog wipes K_temp on expiry. Pending packs become `status='stuck_no_key'`. Dispatcher refuses to run on stuck packs.
- **TTL choice:** operator picks at launch. 1h = paranoid (campaign must complete in an hour or get refreshed). 24h = default. 7d = long campaigns / on-vacation tolerance.

### §2.4 - Crypto stack

- **Signing + KEM:** ed25519 + X25519 (32-byte private keys)
- **Symmetric:** AES-256-GCM (32-byte K_temp)
- **Library:** libsodium / NaCl primitives (widely audited, fast, no algorithm choices to fuck up)
- **Envelope (optional aesthetic):** keys can be displayed as 512-char base64 strings in setup output for visible-paranoia. Crypto material is still 32-byte primitives underneath.

---

## §3 - Sync protocol (the genuinely new build)

### §3.1 - Wire format (every record on the sync wire)

```json
{
  "table":             "templates|campaigns|contacts|suppression|...",
  "id":                "<uuid>",
  "revision":          47,
  "actor":             "operator|client",
  "modified_at":       "2026-05-19T01:23:45Z",
  "encrypted_payload": "<base64>",
  "key_id":            "K_op+K_cl" | "K_temp",
  "deleted":           false
}
```

Cloud stores blob + plaintext metadata (`revision`, `actor`, `modified_at`, `table`, `id`, `key_id`, `deleted`). Cloud sorts/indexes on metadata; cannot read payloads it doesn't have the key for.

### §3.2 - Lamport revision math (deterministic last-write-wins)

- Each terminal holds local counter
- On write: `counter = max(local_counter, max_observed_revision) + 1`
- On sync receive: `local_counter = max(local_counter, max_received_revision)`
- Tie-breaker on equal revisions: `operator > client` (lexically deterministic)

Wall clock used **only** for sub-minute conflict-flag UX. Never for ordering.

### §3.3 - Transport (long-poll)

```
GET  /sync?since=<event_id>&timeout=30
  -> returns events with event_id > since immediately if any
  -> holds up to 30s otherwise; returns [] on timeout
  -> terminal repeats with new cursor

POST /sync/push
  body: [array of sync events]
  -> cloud validates revisions, assigns event_ids, stores, returns acks
  -> cloud detects sub-minute conflicts -> writes near_conflict_log row
```

Long-poll over HTTP/2. No WebSocket, no persistent connections to maintain. Reconnect = re-issue GET.

### §3.4 - Cursor / resume

Cloud maintains a global `event_id` sequence (monotonic). Terminal stores `last_synced_event_id` locally. Reconnect = resume. Idempotent - replaying events you've already seen no-ops (local DB has higher revision).

### §3.5 - Conflict flagging (UX-only, doesn't change ordering)

Near-conflict = two writes to same `(table, id)` whose `modified_at` are within 60s of each other.

Cloud detects on `/sync/push` receipt -> writes to `near_conflict_log`. Both terminals see the flag on next sync. UI surfaces as:

> *"You and [other party] both touched template 'welcome' 47 seconds apart. Last write won (yours). Review to confirm that was intentional."*

Last-write-wins by revision still applies. The flag is advisory and does not
change ordering.

### §3.6 - Tombstones

Delete = `deleted: true` + new revision. Cloud keeps metadata forever. Optional GC: once both terminals' cursors past tombstone's event_id, cloud may drop `encrypted_payload` (metadata stays).

---

## §4 - Pack lifecycle (send execution)

### §4.1 - Launch flow

```
[LOCAL TERMINAL - campaign launch click]
  1. Validate launch gate (template hash + suppression + tested flag)
  2. Build packs locally (one per recipient, fully rendered HTML+text)
  3. For each pack:
       - Encrypt with K_temp (current active)
       - Include: recipient, subject, html, text, headers,
         resend_api_key (per-domain), send_at, content_hash
  4. POST /packs with the batch
  5. ALSO: write `dispatched_job` records via regular sync (K_op+K_cl encrypted)
     - so both terminals see the launch in audit
```

### §4.2 - Cloud dispatcher loop

```
LOOP:
  pack = SELECT * FROM packs
         WHERE send_at <= now() AND status='pending' AND key_id=<current K_temp id>
         ORDER BY send_at
         LIMIT 1 FOR UPDATE SKIP LOCKED
  if !pack: sleep(0.1s); continue

  try:
    decrypt(pack.encrypted_payload, K_temp)   # plaintext window starts
    resp = POST resend.com with pack contents
    plaintext = None                          # plaintext window ends (~ms)
    if resp.ok:
        UPDATE pack SET status='sent', resend_message_id=resp.id,
                       encrypted_payload=NULL, sent_at=now()
    else:
        UPDATE pack SET status='failed', failure=resp.error,
                       encrypted_payload=NULL
  except DecryptError:
    # K_temp expired or wrong-key - refuse to retry
    UPDATE pack SET status='stuck_no_key'
```

**Plaintext window:** measurable in milliseconds. The dispatcher claim -> decrypt -> POST -> discard sequence runs synchronously; plaintext is never persisted, never logged, never visible outside the dispatcher's stack frame.

### §4.3 - Webhook receipt (the special case)

```
POST /webhooks/resend                # public endpoint, HMAC verified
  1. svix HMAC verification with RESEND_WEBHOOK_SECRET
  2. Fail-closed if secret unset
  3. Parse event
  4. Encrypt event payload to BOTH K_op_pub AND K_cl_pub
     (using per-event ephemeral key + dual wrap, per §2.2)
  5. Write as a sync event (table='events', K_op+K_cl mode)
  6. Either terminal picks up on next sync
```

Webhooks survive past K_temp TTL because they're encrypted with the long-lived public keys, not K_temp.

---

## §5 - Public endpoints (cloud-side, internet-facing)

| Endpoint | Purpose | Auth |
|---|---|---|
| `POST /webhooks/resend` | Resend event ingestion | HMAC (svix) |
| `GET /u/<token>` | RFC 8058 unsub landing | HMAC token |
| `POST /u/<token>` | RFC 8058 one-click POST | HMAC token |
| `GET /e/<token>` | GDPR erasure landing | HMAC token |
| `POST /e/<token>` | GDPR erasure POST | HMAC token |
| `GET /healthz` | bare health check | none |

Token HMAC verification uses `UNSUB_SECRET` and `ERASURE_SECRET` - both shipped to cloud at init handshake, encrypted with K_temp (rotate when K_temp rotates).

---

## §6 - Sync API (cloud-side, terminal-facing, HTTPS + auth)

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /sync?since=N&timeout=30` | long-poll for new events | bearer (per-terminal) |
| `POST /sync/push` | terminal writes | bearer |
| `POST /packs` | upload encrypted send-jobs | bearer |
| `GET /packs/status?since=N` | pull dispatch results | bearer |
| `POST /key/temp` | terminal provisions new K_temp | bearer + signature |
| `DELETE /key/temp` | manual revoke (auto on TTL) | bearer + signature |
| `POST /init/handshake` | one-time setup (registers pubkeys) | bootstrap token |

**Bearer auth:** each terminal holds a long random session token derived from its private key during init. Cloud knows `(K_op_pub, K_cl_pub)` and which bearer maps to which actor. Bearer tokens rotate on K_temp refresh.

---

## §7 - Init handshake script

`scripts/v3 init-handshake` - interactive script run ONCE, during a one-time screen-share session between operator + client.

```
Operator runs:    scripts/v3 init-handshake --role operator --cloud https://mail.example.com
Client runs:      scripts/v3 init-handshake --role client   --cloud https://mail.example.com

Both scripts:
  1. Generate own (K_priv, K_pub) keypair
  2. Print own K_pub for the other party to paste in
  3. Wait for other party's K_pub input
  4. Once both pubkeys exchanged: register with cloud via /init/handshake
     (cloud receives bootstrap token from first connection, validates pubkeys,
     issues bearer tokens for both)
  5. Operator's terminal: receives K_cl_priv from client (out-of-band, via the
     screen-share - operator pastes client's exported private key into operator's
     terminal, and vice versa). Per Reading A - each terminal holds both keys.
  6. Both terminals fire one-click backup IMMEDIATELY
     -> both PCs have full snapshot of post-handshake state
  7. Each terminal additionally exports an offline backup file
     -> goes to 2 physical media (USB, encrypted external drive, etc.)
  8. Total backup locations after init: 6 (2 PC mirrors + 2 offline media + cloud + remote)
```

**Why 6 locations:** "data only lost if 2 offline backups + 2 PC backups + clients + server all compromised simultaneously."

---

## §8 - Multi-domain support (3 sender domains)

- Each sender domain = one row in vault's `domain` table (already supported in schema)
- Each domain has its own `resend_api_key_id` pointer
- Each domain has its own `webhook_secret` (Resend issues per webhook config)
- Packs include the Resend key for the sending domain (so dispatcher picks right key without needing config lookup at send time)
- Operator picks rotation strategy per campaign: `round_robin` / `lru` / `fixed_per_campaign`

**Currently:** client has 2 domains, third TBD. Platform runs on 2 gracefully; adding 3rd is just a new `domain` row + DNS records + Resend verification.

---

## §9 - One-click backup model

Each local terminal exposes a `scripts/v3 backup` verb that produces:

1. Encrypted snapshot of local SQLite (AES-256-GCM with operator-chosen passphrase)
2. Git snapshot of the local repo state (`git bundle create`)
3. Combined zip with versioned filename: `v3-backup-<terminal-id>-<timestamp>.zip`

**6-location target after setup:**

| # | Location | Refresh trigger |
|---|---|---|
| 1 | Operator PC local SQLite | every sync |
| 2 | Client PC local SQLite | every sync |
| 3 | Cloud server encrypted blobs | every sync |
| 4 | Operator PC backup zip | scripts/v3 backup |
| 5 | Client PC backup zip | scripts/v3 backup |
| 6 | Offline media (USB drive at $somewhere) | weekly manual |

Data loss requires simultaneous compromise of all 6.

---

## §10 - Edge-case test + stress harness

**These ship alongside the implementation. Not optional.**

### §10.1 - Sync edge cases

- `tests/sync/test_offline_reconnect.py` - terminal disconnects, makes edits, reconnects, verify all writes propagate
- `tests/sync/test_simultaneous_writes.py` - both terminals edit same row within 60s, verify last-write-wins by revision + near_conflict_log entry
- `tests/sync/test_resume_from_cursor.py` - kill terminal mid-pull, restart, verify resume from correct event_id
- `tests/sync/test_lamport_clock.py` - verify revision counters advance correctly under concurrent writes
- `tests/sync/test_tombstone_propagation.py` - delete on terminal A, verify terminal B sees deletion

### §10.2 - K_temp lifecycle

- `tests/keys/test_auto_mint_on_first_connect.py` - fresh cloud, terminal connects, K_temp auto-created
- `tests/keys/test_ttl_expiry.py` - fast-forward time past TTL, verify cloud wipes K_temp + stuck packs flagged
- `tests/keys/test_refresh_during_active_send.py` - refresh K_temp mid-campaign, verify stuck packs re-encrypted + resumed
- `tests/keys/test_decrypt_with_wrong_key.py` - pack encrypted with old K_temp, dispatcher fails closed

### §10.3 - Pack lifecycle

- `tests/packs/test_dispatcher_microsecond_window.py` - instrument dispatcher, verify plaintext lives <10ms
- `tests/packs/test_cloud_restart_with_inflight.py` - kill cloud mid-dispatch, restart, verify claimed->pending reset
- `tests/packs/test_resend_5xx_retry.py` - mock Resend 5xx, verify pack stays pending + retries
- `tests/packs/test_resend_4xx_no_retry.py` - mock Resend 4xx, verify pack marked failed + no retry
- `tests/packs/test_decrypt_failure_handling.py` - corrupted pack, verify dispatcher logs + moves on

### §10.4 - Webhook edge cases

- `tests/webhooks/test_hmac_rejection.py` - unsigned webhook -> 401
- `tests/webhooks/test_replay_window.py` - old timestamp -> 400
- `tests/webhooks/test_dual_pubkey_encryption.py` - verify event encrypted to both K_op_pub + K_cl_pub
- `tests/webhooks/test_webhook_during_both_offline.py` - webhook arrives while both terminals offline, terminals sync later, both can decrypt

### §10.5 - Adversarial probes (the "stressers")

- `bin/_probe/inject_unsigned_event.sh` - push a sync event with bad signature -> cloud rejects
- `bin/_probe/inject_pack_with_no_key.sh` - push pack referencing non-existent K_temp -> dispatcher catches
- `bin/_probe/replay_old_revision.sh` - push event with revision < current -> no-op (idempotent)
- `bin/_probe/poison_pubkey.sh` - attempt to register a different K_op_pub mid-session -> cloud rejects (handshake is one-shot)
- `bin/_probe/exfil_attempt.sh` - simulate compromised cloud, attempt to read K_op+K_cl encrypted payloads -> cryptographic failure
- `bin/_probe/sync_flood.sh` - terminal pushes 10K writes per second -> cloud handles or rate-limits cleanly

---

## §11 - What's removed from v3.0

| Component | Status in v3.1 |
|---|---|
| WireGuard tunnels (wg1, wg2) | **REMOVED** - sync over HTTPS instead |
| wake-hook service | **REMOVED** - cloud always on; terminals user-toggled via `docker compose up` |
| backup module (separate host) | **REMOVED** - folded into each local terminal's `scripts/v3 backup` |
| vault -> admin UI | **MOVED** to local terminal; admin UI runs on localhost only |
| front-edge (always-on, public) | **MERGED** into cloud server |
| Three-container topology per deploy | **REPLACED** by 1 cloud + 2 local terminals |

## §12 - What's reused from v3.0 codebase

- `vault/app/db.py` -> becomes `terminal/app/db.py` (same schema)
- `vault/app/routes_admin.py` -> becomes `terminal/app/routes_admin.py` (same CRUD)
- `vault/app/admin_ui.py` -> becomes `terminal/app/admin_ui.py` (same HTML)
- `vault/app/auth.py` -> becomes `terminal/app/auth.py` (per-party adaptation)
- `vault/app/launch.py` -> becomes `terminal/app/launch.py` (now builds packs locally + encrypts + pushes to cloud)
- `front-edge/app/webhook_receiver.py` -> moves to `cloud/app/webhook_receiver.py`
- `front-edge/app/compliance_public.py` -> moves to `cloud/app/compliance_public.py`
- `front-edge/app/compliance_tokens.py` -> moves to `cloud/app/compliance_tokens.py`
- `front-edge/app/queue_worker.py` -> becomes `cloud/app/dispatcher.py` (with K_temp decrypt)

**New code needed:**
- `cloud/app/sync.py` - sync endpoints
- `cloud/app/keys.py` - K_temp lifecycle + bearer issuance
- `terminal/app/sync_client.py` - long-poll loop + conflict reconciliation
- `terminal/app/encryption.py` - K_op+K_cl mode + K_temp mode
- `terminal/app/backup.py` - one-click backup zipper
- `scripts/v3 init-handshake` - interactive setup script
- All of `tests/`

---

## §13 - Implementation order (don't deviate)

1. **Spec lock** (this doc) -> commit
2. **Refactor scaffold** - rename `vault/` -> `terminal/`, remove `wake-hook/`, fold `backup/` into terminal, rename `front-edge/` -> `cloud/`
3. **Encryption layer** - `terminal/app/encryption.py` (two modes) + `cloud/app/keys.py` (K_temp)
4. **Sync protocol** - `cloud/app/sync.py` endpoints + `terminal/app/sync_client.py` long-poll loop
5. **Pack lifecycle** - refactor launch to build+push, refactor dispatcher to decrypt+POST
6. **Webhook receiver** - adapt to encrypt-to-both-pubkeys before sync
7. **Init handshake** - `scripts/v3 init-handshake` interactive script
8. **One-click backup** - `scripts/v3 backup` verb
9. **All tests per §10** - written alongside each piece, not at the end
10. **Stress probes per §10.5** - same

**Discipline:** every commit references its §-section in the message (e.g. `feat(cloud/sync): GET /sync long-poll per §3.3`). Drift becomes visible in git log.

---

## §14 - Acknowledged trade-offs (operator decisions)

- **Each terminal holds both private keys** (Reading A) - one-PC compromise = full breach. Mitigated by 6-location backup against data LOSS; not against data DISCLOSURE.
- **Cloud holds K_temp during TTL** - cloud compromise during TTL = read everything K_temp can decrypt. TTL choice scales blast radius.
- **Last-write-wins by Lamport revision** - occasional data loss when both parties edit the same row at once. Acceptable
  because the two operators coordinate directly.
- **Single hardened cloud server** - single point of failure if attackers want sending capability + key access. Mitigated by short TTL choices + audit log + the dispatcher's microsecond plaintext window.
- **No multi-tenant** - one cloud server per engagement. If a second client surfaces, deploy a second cloud server, no shared state.

---

**Origin:** Pivoted from v3.0 (WG-mediated, all-local) during the 2026-05-19 client call. Client wants minimum-touch operations on their side; pivots to operator-hosted cloud + two local terminals. Folds in 3-sender-domain rotation, hybrid key model, sync layer, and the testers/stressers harness operator emphasized.


## §15 - Settings model (Q1 refactor, shipped in v3.1)

### Why

Phase A of Q1: move operator-facing config from `.env` files into a DB-backed
`settings` table so admins can edit via UI rather than env-edit + restart.

### Schema (cloud + terminal, identical)

```sql
CREATE TABLE settings (
  key         TEXT    PRIMARY KEY,
  value       TEXT    NOT NULL,        -- plaintext OR base64(AES-256-GCM-encrypted) if is_secret=1
  is_secret   INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_by  TEXT    NOT NULL DEFAULT 'system'
);
```

### Encryption at rest

Secrets (`SECRET_KEYS` constant per side) are encrypted with AES-256-GCM
using a per-side KEK:

- Cloud: `CLOUD_KEYS_DIR/settings_kek.bin` (0600)
- Terminal: `TERMINAL_KEYS_DIR/settings_kek.bin` (0600)

KEK is auto-generated on first `migrate_from_env()` call; 32 random bytes.
Backed up alongside KEM keys via `scripts/v3 backup`. If KEK is lost, encrypted
settings are unrecoverable (operator must re-set via UI).

**Decrypted only on read.** No long-lived plaintext cache; secrets re-decrypt
on every `settings.get()` call. Non-secrets cached in-process for cheap reads.

### Secret keys

- **Cloud:** `resend_webhook_secret`, `unsub_secret`, `erasure_secret`
- **Terminal:** `admin_password_hash`, `jwt_secret`, `unsub_secret`, `erasure_secret`, `resend_api_key`

`admin_password_hash` is already bcrypt'd but still encrypted at rest for
defense-in-depth (an attacker who reads SQLite can't even brute the bcrypt).

### env-only (by necessity)

- `BOOTSTRAP_TOKEN` (cloud) - chicken-egg with handshake auth
- `CLOUD_URL`, `CLOUD_BEARER`, `TERMINAL_ACTOR` (terminal) - runtime location
- `*_DB_PATH`, `*_KEYS_DIR` - filesystem locations
- `LOG_LEVEL`, `DISABLE_DISPATCHER`, `DISABLE_QUEUE_WORKER` - boot/debug flags
- `DB_ENCRYPTION_KEY` - backup-only, prompt-driven via `scripts/v3 backup`

### Boot-time migration

Both `main.py` startup hooks call `settings.migrate_from_env()` which copies
known env vars into the settings table on first boot. Idempotent - if a key
is already in DB, env is shadowed and logged as "safe to remove".

This means existing v3.0 deployments transparently migrate to v3.1 without
operator action. Once they verify the UI shows the values, they can delete
their `.env` entries.

### Admin UI panels

- `/admin/settings/brand` - company_name, support_email, public_host,
  email_footer_address, email_from
- `/admin/settings/auth` - admin_email, change password, rotate JWT
- `/admin/settings/webhooks` - points to Cloud tab for Resend webhook secret
- `/admin/settings/secrets` - unsub/erasure HMAC secrets + RESEND_API_KEY
  with reveal-on-click (audit-logged)
- `/admin/settings/tuning` - TTLs + session lifetime
- `/admin/settings/cloud` - cross-call to cloud's `/settings` via bearer
  (operator's terminal can see + edit cloud's settings)

### REST API

Both sides expose CRUD endpoints:

- Cloud: `/settings*` - bearer auth (terminal calls these from the Cloud UI tab)
- Terminal: `/admin/settings/api/*` - session-cookie auth

Both: list (secrets redacted), single-read, reveal (audit-logged), upsert, delete.

### Cross-side secret sync (Phase C)

Keys in `CROSS_SIDE_SYNC = {unsub_secret, erasure_secret}` auto-push to cloud
when saved on terminal. HMAC verification on both sides stays in lockstep
without manual double-entry.

Limitations:
- Operator-driven push only; cloud->terminal auto-pull on the other terminal
  is a future enhancement (would use the event_log mechanism with K_op+K_cl
  encryption).
- If a secret is set ONLY via cloud's `/settings` API, the terminal won't
  auto-pull. Operator should always set shared secrets from terminal first.

### Tests

`tests/test_settings.py` - 13 tests covering KEK lifecycle, encryption at rest,
tamper detection, migration idempotency, env-fallback transitional behavior.
All 61 tests pass.
