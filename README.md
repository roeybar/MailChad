# MailMan - email campaigns from your own machine, relayed encrypted

<p><picture><source media="(prefers-color-scheme: dark)" srcset="docs/img/icon-dark.svg"><img src="docs/img/icon.svg" width="120" alt="MailMan icon: a mail carrier holding up an envelope"></picture></p>

MailMan prepares campaigns in a terminal app with local contacts, templates
and encryption keys. The terminal encrypts rendered send jobs before handing
them to the cloud. The cloud dispatcher decrypts those jobs with a temporary
key to call Resend, then clears the stored encrypted payload on finalisation.
Webhook events, and suppressions derived from them, are encrypted to both
terminals' public keys; one-click unsubscribes are stored and synced as SHA-256
email hashes. The terminal schedules batches, applies suppression and controls
approvals.

[![CI](https://github.com/roeybar/MailMan/actions/workflows/ci.yml/badge.svg)](https://github.com/roeybar/MailMan/actions/workflows/ci.yml)

**Status:** package version 4.0.0. October 2026 test baseline: 650 passed, 1
skipped; AWS and Resend are mocked. The real-send path has not been run against
Resend for this release. The admin UI is branded v4; the API, storage and tooling
identifiers (ep-v3-*, the scripts/v3 wrapper, v3-backup files) keep their v3 names.

**Spec:** [`docs/lambda-dynamo-spec.md`](docs/lambda-dynamo-spec.md)

**Client deploy guide:** [`docs/CLIENT_HOSTED_DEPLOY.md`](docs/CLIENT_HOSTED_DEPLOY.md)

---

## What this is

Email platform with strict security separation across three components:

- **ep-api Lambda** - public HTTP API (API Gateway -> Lambda).
  Handles sync, handshake, packs push, webhook, compliance and settings.
  Stores public keys and TTL-bounded K_temp in DynamoDB.
- **ep-dispatcher Lambda** - SQS-triggered, with an EventBridge due-pack sweep.
  Decrypts packs with K_temp, calls Resend and records dispatch outcomes.
- **terminal** - local admin UI, SQLite state and its own KEM private key.
  The compose port mapping publishes `8011:8000` on all host interfaces.
  Pulls cloud events every 5 seconds by default.
  Quick send and campaign Test send post one message from the terminal
  straight to Resend, outside the cloud relay and its encryption; Quick send
  does not check suppression.

Cloud storage: **DynamoDB single-table** + **SQS** (due-pack queue).
The Lambda code path requires no always-on VPS.

---

## Try it: demo mode (terminal only, no keys)

The terminal runs on its own: the admin UI, contacts, templates and campaigns
work without a cloud, AWS or a Resend key. Sending and sync stay off until a
cloud is connected (the Overview shows "handshake pending").

```bash
# 1. Create .env
cp .env.example .env

# 2. Make a password hash and add the first-boot login to .env.
#    Keep the single quotes around the hash: it contains $ signs.
scripts/v3 admin-bcrypt            # prompts for a password, prints its hash
# then add to .env:
#   ADMIN_EMAIL=demo@example.com
#   ADMIN_PASSWORD_HASH='<the printed hash>'
#   JWT_SECRET=<a long random string, e.g. from: openssl rand -hex 32>

# 3. Start the terminal
scripts/v3 up-terminal

# 4. Open http://localhost:8011/admin and sign in with that e-mail and password

# 5. Stop it
scripts/v3 down
```

---

## Full local stack (not runnable from this repository yet)

The full stack (local DynamoDB, the cloud API and the dispatcher) needs a
`./cloud` build context and a `cloud` compose service that this repository
does not contain, so `up-dev`, `setup-tables` and a local handshake do not run
from a fresh clone (see Known limitations). The intended steps are:

```bash
# 1. Create .env
cp .env.example .env
# Edit: cloud BOOTSTRAP_TOKEN; terminal ADMIN_EMAIL, ADMIN_PASSWORD_HASH,
# JWT_SECRET, TERMINAL_ACTOR and CLOUD_URL

# 2. Request the full local stack (see Known limitations)
scripts/v3 up-dev

# 3. Create DynamoDB table + SQS queue (first time only) (see Known limitations)
scripts/v3 setup-tables  # (see Known limitations)

# 4. Handshake helper (see Known limitations)
scripts/v3 init-handshake --role operator \
                       --cloud http://cloud:8443 \
                       --bootstrap-token <BOOTSTRAP_TOKEN>

# 5. Backup helper (see Known limitations)
scripts/v3 backup

# 6. Admin UI
# Visit http://localhost:8011/admin in your browser
```

---

## Production deploy (Lambda)

See Known limitations before using the deployment and handshake helpers.

```bash
# One-time: create DynamoDB table + SQS queue in prod AWS
scripts/v3 setup-tables  # (see Known limitations)

# Deploy helpers require AWS credentials, table, queue and execution-role ARNs
scripts/v3 deploy-api          # prints a next step to configure API Gateway
scripts/v3 deploy-dispatcher   # first creation wires SQS; asserts a sweep schedule

# Handshake both parties against the API Gateway URL
scripts/v3 init-handshake --role operator \
                       --cloud https://<api-gw-id>.execute-api.us-east-1.amazonaws.com \
                       --bootstrap-token <BOOTSTRAP_TOKEN>

# Tail Lambda logs
scripts/v3 tail-api
scripts/v3 tail-dispatcher
```

Client setup: see [`docs/CLIENT_HOSTED_DEPLOY.md`](docs/CLIENT_HOSTED_DEPLOY.md) -
they only run a terminal, no cloud infra needed.

---

## All `scripts/v3` verbs

```
scripts/v3 up                Start terminal connected to Lambda
scripts/v3 up-dev            Request full local dev stack (see Known limitations)
scripts/v3 up-cloud          Start cloud only (see Known limitations)
scripts/v3 up-terminal       Start terminal only (client deploy)
scripts/v3 down              Stop everything
scripts/v3 bounce            Restart (keeps volumes)
scripts/v3 logs [svc]        Tail terminal logs (pass terminal)
scripts/v3 shell <svc>       Interactive shell (terminal only)
scripts/v3 ps                List terminal containers
scripts/v3 init-handshake    Invoke handshake helper (see Known limitations)
scripts/v3 backup            Invoke backup helper (see Known limitations)
scripts/v3 admin-bcrypt      Print a bcrypt hash for ADMIN_PASSWORD_HASH (needs .env)
scripts/v3 check <name>      Invoke check helpers (see Known limitations)
scripts/v3 probe <name>      Invoke probe helpers (see Known limitations)
scripts/v3 setup-tables      Create DynamoDB table + SQS queue (local or prod) (see Known limitations)
scripts/v3 deploy-api        API deployment helper (see Known limitations)
scripts/v3 deploy-dispatcher Dispatcher deployment helper (see Known limitations)
scripts/v3 tail-api          CloudWatch log tail for ep-api
scripts/v3 tail-dispatcher   CloudWatch log tail for ep-dispatcher
scripts/v3 wire-sqs-trigger  Create a missing dispatcher SQS mapping (see Known limitations)
scripts/v3 test [args]       Run pytest in the isolated test container
```

---

## Configuration model

Terminal login needs `ADMIN_EMAIL`, `ADMIN_PASSWORD_HASH` and `JWT_SECRET` on
first boot. Cloud handshake needs `BOOTSTRAP_TOKEN`; infrastructure uses AWS,
DynamoDB and SQS configuration. Settings migration reads known environment
variables without overwriting existing rows.

| Page | Settings |
|---|---|
| Entities | Brand, support email, postal address, public host, sender, Resend key |
| Operators | Password change, session lifetime, JWT rotation |
| Suppression | Unsubscribe/erasure secrets and token TTLs |
| Overview | K_temp TTL |
| Sending | Window, pacing and batch size |

Secrets are AES-256-GCM encrypted at rest with a per-side KEK.

---

## Code layout

```
src/mailman/
  terminal/       local admin UI + sync client (runs on an operator's machine)
    migrations/   SQLite schema migrations
  cloud/          public API + dispatcher (deploys as two AWS Lambdas)
deploy/           Dockerfiles, entrypoints, IAM policies, Lambda packaging
scripts/          v3 CLI (up/down/deploy/backup/probe) + helpers
tests/            pytest suite - runs the whole codebase in one env
docs/             cloud, deployment and test guides
```

Both sides are subpackages of one installable package (`src/` layout), so
`mailman.terminal` and `mailman.cloud` import unambiguously and the full test
suite runs in a single environment.


## Hard rules

1. **Long-term KEM keys stay in terminal key storage.** The loader uses its own private key and the peer's public key.
2. **K_temp is TTL-bounded.** Cloud accepts 1h/24h/7d and checks expiry when reading the key.
3. **Dispatch decrypts to call Resend.** Finalisation clears the encrypted payload.
4. **Bearer auth on sync/pack/key endpoints.** Bearer SHA-256 hashes live in DynamoDB.
5. **Webhook events encrypted to both pubkeys** when both are registered.
6. **Settings secrets encrypted at rest** with a per-side KEK; decrypted on read.

---

## Tests

```bash
# Runs the suite in the project's own test container (no host Python)
bash scripts/v3 test
bash scripts/v3 test tests/test_schedule_utc.py -rA     # one file
```

CI also runs `ruff check --select E9,F63,F7,F82` (syntax errors and undefined names).

October 2026 baseline: 650 passed, 1 skipped. Areas covered:
- cloud: DynamoDB ops, the dispatcher (SQS record -> decrypt -> mocked Resend ->
  sent / failed / unknown), the packs API (push, idempotency, cancel), sync,
  handshake, webhooks, settings and encryption round-trips
- terminal: launch and re-launch, batches and approvals, the scheduler (time
  comparison, refusals, stages), archive and cancel, the test send, the From
  header, the admin UI routes and the ORM schema
- long seeded walks over the send path and over archive and cancel

Resend and AWS are mocked in the suite. The real-send path has not been run
against Resend for this release.

---

## Trade-offs (acknowledged)

- Each terminal loads its own long-term private key and the peer's public key;
  it decrypts the wrap addressed to its own role.
- Cloud holds K_temp during its TTL and can read pack payloads encrypted with it.
- Incoming webhook and suppression events are materialised, but general
  contact/template/campaign edits are not merged between terminals.
- The default configuration uses one DynamoDB table and fixed Lambda names.

---

## Known limitations

Sending
- Archiving a campaign while the cloud is unreachable cancels its local batches,
  but mail already handed to the cloud stays pending, and a later Archive does
  not retry cancellation. Cancel the campaign while the cloud is reachable instead.
- Resuming or retrying a campaign and then pressing Launch rebuilds batch
  assignments and can release waiting recipients without their batch's approval.
- Stages and Re-launches while batches await approval can leave those batches stuck.
- Stage packs carry the campaign's scheduled time as their send time.
- Cancelling a pack whose push has not landed yet can answer "not found" and
  does not stop the later push.
- A send whose outcome is unknown is shown as "unknown" and never re-sent; it is
  not reconciled with Resend's own records.
- Batch drain is based on the last scheduled send time, not confirmed delivery.
- Drain in-flight sends before upgrading the terminal: a send retried after an
  upgrade is not deduplicated against one pushed before it.

Operator UI and API
- Archive is one-way: an archived campaign is cloned to send it again.
- Adding a deleted contact to a campaign answers a server error instead of a
  clear refusal.
- The entity set-key API accepts an empty Resend key over a working one.
- The cloud settings form reaches the generic handler, which saves its value
  under the local setting key `cloud`.
- Several admin JSON list routes are shadowed by HTML pages of the same path.
- The overview's sync card shows missing/stale pulls but does not display the
  underlying logged sync error.
- The Overview's Snapshot to backup button runs a backup helper the terminal image does not contain.

Privacy
- Erasure requests recorded by the cloud are not pulled to the terminal by the
  active sync path. The legacy WireGuard path can deadlock on its audit write.
  Check erasure requests by hand.
- Unsubscribe-link suppressions are kept in DynamoDB as unsalted SHA-256 email
  hashes, not encrypted to the terminals' keys.

Tooling
- There is no automated restore command; backups are restored by hand.
- The live e2e script's real-send leg is not wired: its gate always skips
  (see docs/LIVE-E2E.md).
- The dev compose file builds from a `./cloud` directory that no longer exists.
- `scripts/v3 logs`, `shell` and `ps` use the terminal-only production compose
  file and do not reach the dev-stack services.
- `scripts/v3 setup-tables` builds the `cloud` service from the terminal-only
  production compose file, which does not define that service.
- `scripts/v3 wire-sqs-trigger` runs the `email-platform-v3-cloud` image tag,
  which nothing builds.
- `scripts/_check/{drift,healthz,isolation,secret-locality,suppression-mirror,vault-sleep}`
  target the retired v2 vault/backup topology; neither compose file defines
  `ep-v3-vault`, `ep-v3-backup` or `ep-v3-front-edge`.
- The local dispatcher's `run_forever()` imports `dispatcher_lambda` as a
  top-level module, while that module uses `mailman.cloud` imports.
- `scripts/v3 init-handshake` and `backup` mount files from an absent `bin/`
  directory and use an image tag and volume name that compose does not create.
- `scripts/v3 deploy-api` and `deploy-dispatcher` packaging refers to absent
  `cloud` and unmounted `/repo` paths.
- `scripts/v3 check` and `probe` reference absent `bin/_check/` and `bin/_probe/` directories.
- The handshake helper's manual private-key exchange reminder conflicts with
  keeping each terminal's private keys local.

---

## Heritage

- **v1** - AWS Amplify. 8 AWS services.
- **v2** - state/sender split via WireGuard.
- **v3.0** - cloud-sync spinnable; all secrets env-driven.
- **v3.1** - settings moved to DB-backed + UI-editable. Client-hosted-deploy guide.
  Cloudflare Tunnel model.
- **v3.2** - Lambda + DynamoDB. No always-on VPS. Dispatcher decoupled to SQS.
  Client runs terminal only; operator deploys two Lambdas once.
- **v3.8** - Multi-company (entities) support; a live e2e script
  (see docs/LIVE-E2E.md).
- **v3.9** - MailMan design system (oklch, light/dark, icon nav). Settings moved
  inline to feature pages. JSON/HTML template import.
- **v4.0** - SQLAlchemy models, send-path fixes, containerised tests; admin UI
  branded v4.
