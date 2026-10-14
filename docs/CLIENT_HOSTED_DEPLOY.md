# Client deployment guide

The production compose file runs a terminal connected to a cloud API via
`CLOUD_URL`. Cloud code supports an HTTP API Lambda and a dispatcher Lambda.
The terminal does not host those Lambdas. Its compose port mapping is
`8011:8000` without a localhost-only host binding.

---

## What you need

- A laptop or desktop that can run Docker.
- The two values the operator sends you over Signal (or another secure
  channel):
  - `CLOUD_URL` - the API Gateway URL, e.g.
    `https://<id>.execute-api.us-east-1.amazonaws.com`
  - `BOOTSTRAP_TOKEN` - cloud token for registration and role revocation

First-boot login also needs `ADMIN_EMAIL`, `ADMIN_PASSWORD_HASH` and `JWT_SECRET`.

---

## One-time setup

### 1. Prepare Docker and the checkout

The wrapper uses Docker Compose for the terminal.

```bash
git clone <repo-url-from-operator> email-platform
cd email-platform
```

### 2. Create your .env

Copy `.env.example` to `.env`, then set `TERMINAL_ACTOR=client` and the cloud's
`CLOUD_URL`. Set `ADMIN_EMAIL`, a bcrypt `ADMIN_PASSWORD_HASH` and a random
`JWT_SECRET` for initial login. Password-hash helper: `scripts/v3 admin-bcrypt`
(see Known limitations in the README). The handshake requires the cloud's `BOOTSTRAP_TOKEN`.
The terminal startup migrates known env settings without overwriting existing
settings rows.

### 3. Start your terminal

```bash
scripts/v3 up-terminal
```

Your admin UI is now at `http://localhost:8011`. The cloud is Lambda; you
are not running the cloud container at all.

### 4. Run the handshake (screen-share with the operator)

Handshake helper: `scripts/v3 init-handshake --role client --cloud <CLOUD_URL> --bootstrap-token <BOOTSTRAP_TOKEN>`
(see Known limitations in the README). The helper is `scripts/_init_handshake.py`.

That helper generates or reuses the local X25519 keypair, registers the public
key and saves the cloud bearer. It prints the local public key, prompts for
the peer's public key and checks it against `/pubkeys` when registered.
The terminal's key loader uses its own private key and the peer's public key.

The sync module loads its bearer at import time, so a newly saved bearer needs
a terminal process restart before the running sync loops use it.

### 5. Backup helper

See Known limitations in the README before using the wrapper:

```bash
scripts/v3 backup
```

`scripts/_backup.py` encrypts snapshots of SQLite and the key directory and can
include a DynamoDB scan.

### 6. Configure your terminal via the admin UI

Open `http://localhost:8011/admin/auth/login`.

Inline configuration lives on the feature pages:
- **Entities** - brand, support email, postal address, public host and sender
- **Operators** - password change, session TTL and JWT rotation
- **Suppression** - unsubscribe/erasure secrets and token TTLs
- **Overview** - K_temp TTL
- **Sending** - window, pacing and batch size

Set required cloud values through cloud `/settings` or its supported environment
configuration. See Known limitations in the README for the Entities cloud settings form.

---

## Day-to-day operation

- The terminal compose service uses `restart: unless-stopped`.
- Sync is automatic - your terminal polls the Lambda every 5 seconds.
- You can edit contacts, templates and campaigns locally.
- Incoming `webhook_event` and `suppression` rows are materialised; general
  contact/template/campaign edits are not merged between terminals.
- The cloud dispatcher processes packs already uploaded by the terminal.

If you need to restart: `scripts/v3 down && scripts/v3 up-terminal`

---

## Cloud setup helpers

For reference (you don't need to do any of this):

1. `deploy/setup_tables.py` creates the DynamoDB table, SQS queue and DLQ.
2. `deploy/deploy_lambda.py` creates or updates the API and dispatcher Lambdas.
   The API helper prints a next step to configure API Gateway; it does not
   create the gateway or return its URL.
3. First dispatcher creation wires SQS; each deployment asserts the sweep schedule.
4. The cloud handshake reads `BOOTSTRAP_TOKEN` from its environment. Webhook
   verification requires `resend_webhook_secret` or `RESEND_WEBHOOK_SECRET`.

See Known limitations in the README before using the wrapper's deploy helpers.

---

## If something breaks

```bash
scripts/v3 logs terminal       # your terminal logs
scripts/v3 down && scripts/v3 up-terminal   # full restart
```

Send the operator your terminal logs + a description of what you saw.
They can check the Lambda logs with `scripts/v3 tail-api` / `scripts/v3 tail-dispatcher`.

---

## Infrastructure

The production terminal uses a Docker named volume for SQLite and local keys.
Cloud persistence uses DynamoDB; due packs use SQS. No always-on cloud server is
required for the Lambda code path.

---

## If you decide later to hand things back to the operator

Cloud `DELETE /init/handshake/{role}` revokes that role's public key and bearer
sessions and requires `X-Bootstrap-Token`. There is no terminal UI revoke flow.
Stopping a terminal does not cancel packs already uploaded to the cloud.
General contact/template/campaign edits are not materialised on the other
terminal, so do not assume it has a full copy.
