# Client Deployment Guide - $0 cloud infrastructure

As of v3.2 the cloud runs on **AWS Lambda + DynamoDB** instead of a Docker
container. The operator deploys the Lambda once; you (the client) just run
the terminal on your laptop pointed at the API Gateway URL the operator gives
you. No server to host, no Cloudflare Tunnel, no open ports.

---

## What you need

- A laptop or desktop that can run Docker.
- The two values the operator sends you over Signal (or another secure
  channel):
  - `CLOUD_URL` - the API Gateway URL, e.g.
    `https://<id>.execute-api.us-east-1.amazonaws.com`
  - `BOOTSTRAP_TOKEN` - one-time token used during handshake only

That's it. No AWS account, no DNS, no VPS.

---

## One-time setup

### 1. Install Docker

Linux: `curl -fsSL https://get.docker.com | sudo sh`
Mac: install Docker Desktop
Windows: Docker Desktop with WSL2

Verify: `docker --version`

### 2. Clone the platform

```bash
git clone <repo-url-from-operator>
cd email-platform-v3
```

### 3. Create your .env

```bash
cat > .env <<EOF
BOOTSTRAP_TOKEN=<paste from operator>
TERMINAL_ACTOR=client
CLOUD_URL=<paste API Gateway URL from operator>
LOG_LEVEL=INFO
EOF
chmod 600 .env
```

`CLOUD_URL` is the only difference from the operator's `.env` - it points
at the Lambda the operator already deployed.

### 4. Start your terminal

```bash
scripts/v3 up-terminal
```

Your admin UI is now at `http://localhost:8011`. The cloud is Lambda; you
are not running the cloud container at all.

### 5. Run the handshake (screen-share with the operator)

Coordinate a 10-minute call. During the call, run:

```bash
scripts/v3 init-handshake \
  --role client \
  --cloud <CLOUD_URL> \
  --bootstrap-token <BOOTSTRAP_TOKEN>
```

The script will:
- Generate your X25519 keypair and store it locally
- Register your public key with the cloud Lambda
- Give you a bearer token - stored automatically, no action needed
- Print your public key for the operator to copy
- Prompt you to paste the operator's public key

Both parties end the call with each other's pubkeys registered. The terminal
sync client will start pulling events from the cloud immediately after.

### 6. Backup immediately

```bash
scripts/v3 backup
```

Copy the zip to two separate places (USB drive, cloud storage, etc). The zip
contains your encryption keys. Per the architecture, data is only lost if
ALL backups AND the cloud are wiped simultaneously.

### 7. Configure your terminal via the admin UI

Open `http://localhost:8011/admin/auth/login`.

Settings -> fill in:
- **Brand** - your company name, support email, postal address (CAN-SPAM),
  and public hostname (the operator's API Gateway domain)
- **Auth** - set a strong admin password

The operator handles cloud-side settings (Resend, webhook secret, etc) from
their terminal.

---

## Day-to-day operation

- Open your laptop -> terminal is already running (it restarts automatically
  via Docker's `restart: unless-stopped`).
- Sync is automatic - your terminal polls the Lambda every 5 seconds.
- You can edit contacts, templates, and campaigns from your admin UI.
- The operator can do the same from theirs - last-write-wins per field;
  near-conflicts flagged for review.
- Sends are dispatched by the Lambda dispatcher - no action needed on your end.

If you need to restart: `scripts/v3 down && scripts/v3 up-terminal`

---

## What the operator set up on their end

For reference (you don't need to do any of this):

1. Created an AWS account + IAM user with the policies in `deploy/iam/`
2. Ran `scripts/v3 setup-tables` to create the DynamoDB table + SQS queue
3. Ran `scripts/v3 deploy-api` -> API Gateway URL pointing at the ep-api Lambda
4. Ran `scripts/v3 deploy-dispatcher` -> ep-dispatcher Lambda triggered by SQS
5. Set `BOOTSTRAP_TOKEN` as a Lambda environment variable before your handshake

After the handshake, BOOTSTRAP_TOKEN has served its purpose - the operator
can remove it from the Lambda env if they want.

---

## If something breaks

```bash
scripts/v3 logs terminal       # your terminal logs
scripts/v3 down && scripts/v3 up-terminal   # full restart
```

Send the operator your terminal logs + a description of what you saw.
They can check the Lambda logs with `scripts/v3 tail-api` / `scripts/v3 tail-dispatcher`.

---

## Cost breakdown

| Item | Cost |
|---|---|
| AWS Lambda (ep-api + ep-dispatcher) | $0 - free tier covers <1M invocations/mo |
| AWS DynamoDB | $0 - free tier covers <25 RCU/WCU, always |
| AWS SQS | $0 - free tier covers 1M messages/mo, always |
| AWS API Gateway HTTP | $0 first 12 months; $0.01/mo after (at 10K req/mo) |
| Terminal (your laptop, Docker) | $0 marginal |
| Resend (≤3,000 emails/mo) | $0 |
| Resend Pro (50K/mo) | $20/mo |
| **Platform total at 3K send** | **$0/mo** |
| **Platform total at 50K send** | **~$20/mo** |

No VPS. No Cloudflare subscription. No DNS bill.

---

## If you decide later to hand things back to the operator

Nothing is locked in. If you want to stop running even the terminal:

1. Do one final `scripts/v3 backup` and send it to the operator.
2. `scripts/v3 down`
3. The operator can revoke your bearer from their terminal.

Your contact data lives in DynamoDB (operator's AWS account) and in your
local terminal DB. The operator already has a full copy via sync.
