"""Campaign launch - terminal side, v3.1 refactor (§4.1, §13.5).

Replaces v3.0's launch which pushed to front-edge /queue. v3.1 builds
encrypted packs locally + pushes to cloud /packs.

Algorithm:
  1. Validate launch gate (template hash + suppression + tested flag).
  2. Ensure K_temp is provisioned with cloud (mint + push if absent).
  3. For each recipient:
     - Render content + mint per-recipient unsub/erasure tokens
     - Build a plaintext pack dict: recipient, subject, html, text, headers,
       resend_api_key (per-domain), from
     - Encrypt with K_temp via encryption.encrypt_with_temp
     - Add to batch
  4. POST batch to cloud /packs
  5. Write dispatched_job rows locally (for drift detection)
  6. Update campaign.status = 'dispatched'
  7. Audit-log

Token minting uses the same HMAC scheme as cloud/app/compliance_tokens.py
(shared UNSUB_SECRET + ERASURE_SECRET env vars).
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import time
import uuid
from datetime import datetime, timezone

import httpx

from mailchad.terminal import db, encryption, settings, send_schedule

log = logging.getLogger("terminal.launch")

CLOUD_URL = os.environ.get("CLOUD_URL", "http://cloud:8443")   # infra-bound, env-only


class LaunchError(Exception):
    pass


# Token minting (matches cloud/app/compliance_tokens.py)

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _secret(kind: str) -> bytes:
    key = "unsub_secret" if kind == "unsub" else "erasure_secret"
    s = settings.get(key, "") or ""
    if not s:
        return hashlib.sha256(f"dev-only-{kind}-secret".encode()).digest()
    return s.encode()


def mint_token(kind: str, email_hash: str, ttl_s: int) -> str:
    payload = {"h": email_hash, "n": _secrets.token_urlsafe(8), "x": int(time.time()) + ttl_s}
    payload_b = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = _b64u(payload_b)
    sig = hmac.new(_secret(kind), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u(sig)}"


# Rendering

def _render(template: dict, contact: dict) -> tuple[str, str, str]:
    subject = template["subject"]
    html = template["html_body"]
    text = template.get("text_body") or ""
    name = contact.get("name", "") or ""
    first = name.split()[0] if name else ""
    vars_ = {
        "email": contact["email"], "name": name, "first_name": first,
        "firstName": first, "full_name": name, "fullName": name,
    }
    for k, v in vars_.items():
        subject = subject.replace(f"{{{{{k}}}}}", v)
        html = html.replace(f"{{{{{k}}}}}", v)
        text = text.replace(f"{{{{{k}}}}}", v)
    return subject, html, text


def _html_to_text(html: str) -> str:
    import re
    t = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    t = re.sub(r"</p>", "\n\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _inject_compliance(html: str, text: str, unsub_url: str, erasure_url: str,
                        kind: str) -> tuple[str, str]:
    footer_addr = settings.get("email_footer_address", "") or ""

    # Substitute any author-placed compliance tokens in the body first, so a
    # template can put the unsubscribe/erasure links where it wants (best practice).
    def _sub(s: str) -> str:
        if not s:
            return s
        for tok in ("{{unsubscribeUrl}}", "{{unsubscribe_url}}", "{{unsubUrl}}"):
            s = s.replace(tok, unsub_url)
        for tok in ("{{erasureUrl}}", "{{erasure_url}}", "{{deleteUrl}}"):
            s = s.replace(tok, erasure_url)
        return s

    had_unsub_token = bool(html and ("unsubscribeUrl" in html or "unsubscribe_url" in html)) \
        or bool(text and ("unsubscribeUrl" in text or "unsubscribe_url" in text))
    html = _sub(html)
    text = _sub(text)

    if kind == "promotional":
        # Only append our footer if the template didn't already carry the unsub link.
        if not had_unsub_token and footer_addr:
            footer_html = (
                f"<hr style='margin:24px 0;border:0;border-top:1px solid #ddd'>"
                f"<p style='font-size:11px;color:#888;line-height:1.5'>"
                f"{footer_addr}<br>"
                f"<a href='{unsub_url}' style='color:#666'>Unsubscribe</a> · "
                f"<a href='{erasure_url}' style='color:#666'>Delete my data</a>"
                f"</p>"
            )
            html = html + footer_html
        if not text:
            text = _html_to_text(html)
        if not had_unsub_token:
            text = text + (
                f"\n\n---\n{footer_addr}\n"
                f"Unsubscribe: {unsub_url}\nDelete my data: {erasure_url}\n"
            )
    elif not text:
        text = _html_to_text(html)
    return html, text


def _list_unsub_headers(unsub_url: str) -> dict:
    return {
        "List-Unsubscribe":      f"<{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


# K_temp lifecycle (terminal-side helpers)

async def _ensure_k_temp(bearer: str) -> bytes:
    """Get an active K_temp. Local cache -> cloud check -> mint fresh if needed."""
    local_path = encryption.KEYS_DIR / "k_temp.bin"
    if local_path.exists():
        k = local_path.read_bytes()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{CLOUD_URL}/key/temp/status",
                headers={"Authorization": f"Bearer {bearer}"},
            )
            status = r.json() if r.status_code == 200 else {"present": False}
        if status.get("present") and status.get("key_id") == encryption.k_temp_id(k):
            return k
        local_path.unlink(missing_ok=True)

    ttl = settings.get_int("default_k_temp_ttl_s", 86400)
    k = encryption.mint_k_temp()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{CLOUD_URL}/key/temp",
            json={"k_temp_b64": base64.b64encode(k).decode(), "ttl_seconds": ttl},
            headers={"Authorization": f"Bearer {bearer}"},
        )
        r.raise_for_status()
    encryption.KEYS_DIR.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(k)
    os.chmod(local_path, 0o600)
    log.info("minted + pushed K_temp (id=%s, ttl=%ss)", encryption.k_temp_id(k), ttl)
    return k


# Launch orchestrator

async def launch_campaign(campaign_id: int, actor: str = "operator:unknown",
                          template_id_override: int | None = None,
                          stage_id: int | None = None,
                          relaunch: bool = False) -> dict:
    """Launch a campaign (or one stage of a multi-stage campaign).

    template_id_override: use a different template (for stages). Skips hash gate.
    stage_id: if set, marks that campaign_stage as dispatched on success.
    """
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    if not bearer_path.exists():
        raise LaunchError("cloud bearer missing - run `bin/v3 init-handshake` first (§7)")
    bearer = bearer_path.read_text().strip()

    with db.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not camp:
            raise LaunchError(f"campaign {campaign_id} not found")
        camp = dict(camp)

        # For stage launches, allow dispatched/tested campaigns; bypass hash gate
        is_stage = stage_id is not None
        if not is_stage and not relaunch:
            if camp["status"] not in ("tested", "scheduled"):
                raise LaunchError(f"campaign status {camp['status']!r}; must be tested or scheduled")
            if not camp.get("test_sent_at"):
                raise LaunchError("test send required before launch (PRD §4.6)")

        tmpl_id = template_id_override or camp["template_id"]
        tmpl = c.execute("SELECT * FROM templates WHERE id = ?", (tmpl_id,)).fetchone()
        if not tmpl:
            raise LaunchError(f"template {tmpl_id} not found")
        tmpl = dict(tmpl)

        if not is_stage and not relaunch:
            if camp.get("template_hash_at_test") != tmpl["template_hash"]:
                raise LaunchError(
                    f"template edited since last test (test={camp['template_hash_at_test'][:8]} "
                    f"cur={tmpl['template_hash'][:8]}); re-test required")
        if camp["kind"] == "promotional" and not settings.get("email_footer_address"):
            raise LaunchError("promotional needs email_footer_address setting (CAN-SPAM)")

        # Stages and relaunches send to all; normal launch sends to queued only
        if is_stage or relaunch:
            status_filter = "AND cr.status IN ('queued','sent','opened','clicked','bounced','complained','failed')"
        else:
            status_filter = "AND cr.status = 'queued'"
        recipients = [dict(r) for r in c.execute(
            f"SELECT cr.contact_id, c2.email, c2.name "
            f"FROM campaign_recipients cr JOIN contacts c2 ON c2.id = cr.contact_id "
            f"WHERE cr.campaign_id = ? {status_filter}",
            (campaign_id,),
        ).fetchall()]
        # Scope-aware suppression: promotional sends are blocked by
        # promotional+all unsubs; transactional sends only by 'all'.
        # Non-unsubscribe reasons (bounce/complaint/manual/erasure) always block.
        sup_rows = c.execute(
            "SELECT email_hash, reason, scope FROM suppression_hashes"
        ).fetchall()
        is_promotional = camp["kind"] == "promotional"
        suppressed = set()
        for sr in sup_rows:
            if sr["reason"] != "unsubscribe":
                suppressed.add(sr["email_hash"])          # hard suppression, any kind
            elif sr["scope"] == "all":
                suppressed.add(sr["email_hash"])          # full unsubscribe
            elif is_promotional:
                suppressed.add(sr["email_hash"])          # promotional-scope blocks promo only

    if not recipients:
        raise LaunchError("no queued recipients")

    # Per-day single-campaign lock: refuse if any recipient is still locked by
    # another in-flight campaign (anti-spam + compliance + no info loss).
    if not is_stage:
        from mailchad.terminal import campaign_lock
        conflicts = campaign_lock.find_conflicts(campaign_id, [r["contact_id"] for r in recipients])
        if conflicts:
            ex = conflicts[0]
            raise LaunchError(
                f"{len(conflicts)} contact(s) are locked by another running campaign "
                f"(e.g. '{ex['campaign_name']}', free at {ex['free_at']} UTC). "
                f"A contact can only be on one campaign per day."
            )

    public_host = settings.get("public_host", "mail.example.com") or "mail.example.com"

    entity_id = camp.get("entity_id")
    if entity_id:
        from mailchad.terminal.routes_admin import get_entity_credentials
        try:
            creds = get_entity_credentials(entity_id)
        except ValueError as e:
            raise LaunchError(str(e))
        resend_key = creds["resend_key"]
        email_from_name = creds["from_name"]
        email_from = creds["from_email"]
        # public_host stays as configured in settings - compliance endpoints
        # are always on the cloud/Lambda, never on the sending domain.
    else:
        resend_key = settings.get("resend_api_key", "") or ""
        email_from = settings.get("email_from", f"noreply@{public_host}") or f"noreply@{public_host}"
        email_from_name = settings.get("from_name", "") or ""

    if not resend_key:
        raise LaunchError("resend_api_key not configured - set via admin UI or on the company")

    k_temp = await _ensure_k_temp(bearer)
    key_id = f"K_temp_{encryption.k_temp_id(k_temp)}"

    ctx = _SendCtx(campaign_id=campaign_id, camp=camp, tmpl=tmpl, public_host=public_host,
                   resend_key=resend_key, email_from=email_from, email_from_name=email_from_name,
                   k_temp=k_temp, key_id=key_id, bearer=bearer)

    # Legacy single-shot path: stages + relaunch (no batching/windowing)
    if is_stage or relaunch:
        send_at = camp.get("scheduled_for") or "1970-01-01T00:00:00Z"
        deliver = [r for r in recipients
                   if hashlib.sha256(r["email"].lower().encode()).hexdigest() not in suppressed]
        skipped = len(recipients) - len(deliver)
        packs, jobs = [], []
        for r in deliver:
            p, j = _build_pack(ctx, r, send_at, human_send=bool(camp.get("human_send")))
            packs.append(p); jobs.append(j)
        ack = await _push_packs(packs, bearer)
        _record_dispatch(campaign_id, jobs, batch_no=None)
        with db.conn() as c:
            if stage_id is not None:
                c.execute("UPDATE campaign_stages SET status='dispatched', "
                          "dispatched_at=datetime('now') WHERE id=?", (stage_id,))
                c.commit()
        summary = {"campaign_id": campaign_id, "dispatched": len(packs),
                   "suppressed": skipped, "total_recipients": len(recipients),
                   "mode": "stage" if is_stage else "relaunch", "cloud_ack": ack}
        db.audit(actor=actor, action="campaign.launched",
                 target=f"campaign:{campaign_id}", details=summary)
        log.info("launched %s (%s): %s", campaign_id, summary["mode"], summary)
        return summary

    # v3.22 batched + windowed path (normal launch)
    deliver = [r for r in recipients
               if hashlib.sha256(r["email"].lower().encode()).hexdigest() not in suppressed]
    skipped = len(recipients) - len(deliver)
    if not deliver:
        raise LaunchError("all recipients suppressed - nothing to send")

    batch_size = settings.get_int("send_batch_size", 1000)
    batches = [deliver[i:i + batch_size] for i in range(0, len(deliver), batch_size)]

    with db.conn() as c:
        c.execute("BEGIN")
        for bi, brecips in enumerate(batches, start=1):
            c.execute("INSERT OR IGNORE INTO campaign_batches "
                      "(campaign_id, batch_no, size, status) VALUES (?,?,?,?)",
                      (campaign_id, bi, len(brecips), "pending"))
            for r in brecips:
                c.execute("UPDATE campaign_recipients SET batch_no=? "
                          "WHERE campaign_id=? AND contact_id=?",
                          (bi, campaign_id, r["contact_id"]))
        c.execute("UPDATE campaigns SET status='dispatched', dispatched_at=datetime('now'), "
                  "lock_until=datetime('now','+'||lock_duration_s||' seconds'), "
                  "unsubs_confirmed_at=NULL, updated_at=datetime('now') WHERE id=?",
                  (campaign_id,))
        c.commit()

    # Dispatch only batch 1 now; batches 2+ wait for manual approve-next.
    batch1 = await dispatch_batch(campaign_id, 1, ctx=ctx)

    summary = {"campaign_id": campaign_id, "batches": len(batches),
               "batch_1": batch1, "suppressed": skipped,
               "total_deliverable": len(deliver),
               "seeds_per_batch": len(_active_seed_emails())}
    db.audit(actor=actor, action="campaign.launched",
             target=f"campaign:{campaign_id}", details=summary)
    log.info("launched %s (batched): %s", campaign_id, summary)
    return summary


# v3.22 batched-send helpers

@dataclasses.dataclass
class _SendCtx:
    campaign_id: int
    camp: dict
    tmpl: dict
    public_host: str
    resend_key: str
    email_from: str
    email_from_name: str
    k_temp: bytes
    key_id: str
    bearer: str


def _active_seed_emails() -> list[str]:
    with db.conn() as c:
        return [r["email"] for r in c.execute(
            "SELECT email FROM seed_addresses WHERE active=1 ORDER BY id"
        ).fetchall()]


def _build_pack(ctx: _SendCtx, r: dict, send_at: str, *, human_send: bool,
                is_seed: bool = False) -> tuple[dict, dict]:
    """Build one wire pack + its dispatched_job record for recipient r at send_at."""
    camp, tmpl, campaign_id = ctx.camp, ctx.tmpl, ctx.campaign_id
    email = r["email"]
    email_hash = hashlib.sha256(email.lower().encode()).hexdigest()
    subject, html, text = _render(tmpl, r)
    unsub_ttl   = settings.get_int("unsub_token_ttl_s",   60 * 60 * 24 * 365)
    erasure_ttl = settings.get_int("erasure_token_ttl_s", 60 * 60 * 24 * 30)
    unsub_token = mint_token("unsub", email_hash, unsub_ttl)
    erasure_token = mint_token("erasure", email_hash, erasure_ttl)
    unsub_url = f"https://{ctx.public_host}/u/{unsub_token}"
    erasure_url = f"https://{ctx.public_host}/e/{erasure_token}"
    html, text = _inject_compliance(html, text, unsub_url, erasure_url, camp["kind"])

    plain_pack = {
        "recipient": email, "subject": subject, "html": html, "text": text,
        "headers": _list_unsub_headers(unsub_url), "from": ctx.email_from,
        "resend_api_key": ctx.resend_key, "campaign_id": campaign_id,
        "rate_limit_per_min": camp.get("rate_limit_per_min"),
        "bounce_pause_pct": camp.get("bounce_pause_pct") or 0.10,
        # v3.22: pacing lives in send_at, so the dispatcher does NOT add jitter.
        "human_send": human_send,
        "human_send_min_s": camp.get("human_send_min_s") or 60,
        "human_send_max_s": camp.get("human_send_max_s") or 210,
        "human_send_count": camp.get("human_send_count") or 1,
    }
    envelope = encryption.encrypt_with_temp(json.dumps(plain_pack).encode(), ctx.k_temp)
    pack_id = str(uuid.uuid4())
    content_hash = hashlib.sha256((subject + "\n" + html).encode()).hexdigest()
    pack = {
        "pack_id": pack_id, "campaign_id": campaign_id,
        "recipient_hash": email_hash, "content_hash": content_hash,
        "send_at": send_at, "key_id": ctx.key_id,
        "encrypted_blob": base64.b64encode(envelope).decode(),
    }
    job = {"pack_id": pack_id, "contact_id": r.get("contact_id"),
           "recipient_hash": email_hash, "content_hash": content_hash,
           "send_at": send_at, "is_seed": is_seed}
    return pack, job


PACK_PUSH_CHUNK = int(os.environ.get("PACK_PUSH_CHUNK", "100"))


async def _push_packs(packs: list[dict], bearer: str) -> dict:
    """Push packs to the cloud in chunks - each pack carries an encrypted HTML payload
    (~12KB), so a full 1000-pack batch is ~10MB+ and exceeds API Gateway's request-size
    limit (413). Chunk at PACK_PUSH_CHUNK and aggregate the acks."""
    agg = {"accepted": 0, "submitted": 0, "enqueued": 0, "deferred": 0}
    if not packs:
        return agg
    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(0, len(packs), PACK_PUSH_CHUNK):
            part = packs[i:i + PACK_PUSH_CHUNK]
            resp = await client.post(f"{CLOUD_URL}/packs", json=part,
                                     headers={"Authorization": f"Bearer {bearer}"})
            if resp.status_code >= 400:
                raise LaunchError(f"cloud /packs rejected ({resp.status_code}) on chunk "
                                  f"{i // PACK_PUSH_CHUNK + 1}: {resp.text[:160]}")
            j = resp.json()
            for k in agg:
                agg[k] += j.get(k, 0)
    return agg


def _record_dispatch(campaign_id: int, jobs: list[dict], batch_no: int | None) -> None:
    """Persist dispatched_job + mark recipients sent. Seeds (no contact_id) are
    fire-and-forget - no recipient/job row (tracked via seed_placements)."""
    with db.conn() as c:
        c.execute("BEGIN")
        for j in jobs:
            if j.get("is_seed") or j.get("contact_id") is None:
                continue
            c.execute(
                "INSERT INTO dispatched_job (job_id, campaign_id, recipient_id, "
                "recipient_hash, content_hash, dispatched_at) "
                "VALUES (?,?,?,?,?,datetime('now'))",
                (j["pack_id"], campaign_id, j["contact_id"], j["recipient_hash"], j["content_hash"]),
            )
            c.execute(
                "UPDATE campaign_recipients SET status='sent', sent_at=datetime('now') "
                "WHERE campaign_id=? AND contact_id=?",
                (campaign_id, j["contact_id"]),
            )
        c.commit()


async def dispatch_batch(campaign_id: int, batch_no: int, ctx: _SendCtx | None = None) -> dict:
    """Compile + load one batch: compute a send_at per recipient (window + senders +
    rush), salt the batch with active seeds, push all packs, and mark the batch
    sending. Self-contained so approve-next can call it for batch K+1. The cloud
    fires each pack when its send_at comes due - terminal uninvolved thereafter."""
    if ctx is None:
        ctx = await _load_send_ctx(campaign_id)

    with db.conn() as c:
        recips = [dict(r) for r in c.execute(
            "SELECT cr.contact_id, c2.email, c2.name "
            "FROM campaign_recipients cr JOIN contacts c2 ON c2.id = cr.contact_id "
            "WHERE cr.campaign_id=? AND cr.batch_no=? AND cr.status='queued'",
            (campaign_id, batch_no),
        ).fetchall()]
        batch_row = c.execute(
            "SELECT id FROM campaign_batches WHERE campaign_id=? AND batch_no=?",
            (campaign_id, batch_no),
        ).fetchone()
    if not recips:
        raise LaunchError(f"batch {batch_no}: no queued recipients")

    seeds = [{"email": e, "name": "", "contact_id": None} for e in _active_seed_emails()]
    total = len(recips) + len(seeds)

    start_after = datetime.now(timezone.utc)
    send_ats = send_schedule.compute_send_ats(
        total, start_after,
        tz=settings.get("send_window_tz", "America/Los_Angeles") or "America/Los_Angeles",
        start_hour=settings.get_int("send_window_start_hour", 9),
        window_hours=settings.get_int("send_window_hours", 4),
        sender_count=settings.get_int("send_sender_count", 3),
        jitter_min_s=settings.get_int("send_jitter_min_s", 60),
        jitter_max_s=settings.get_int("send_jitter_max_s", 210),
        rush_tail_minutes=settings.get_int("send_rush_tail_minutes", 30),
        rush_jitter_s=settings.get_int("send_rush_jitter_s", 15),
        daily_cap=(settings.get_int("send_daily_cap", 0) or None),  # 0 = unlimited
    )

    # Distribute seeds evenly across the batch's schedule so placement is sampled
    # throughout, not all at the start.
    targets: list[tuple[dict, bool]] = [(r, False) for r in recips]
    if seeds:
        step = max(len(targets) // len(seeds), 1)
        for i, s in enumerate(seeds):
            targets.insert(min(i * (step + 1), len(targets)), (s, True))

    packs, jobs = [], []
    for (r, is_seed), send_at in zip(targets, send_ats):
        p, j = _build_pack(ctx, r, send_at, human_send=False, is_seed=is_seed)
        packs.append(p); jobs.append(j)

    ack = await _push_packs(packs, ctx.bearer)
    _record_dispatch(campaign_id, jobs, batch_no=batch_no)

    win_start, win_end = (send_ats[0], send_ats[-1]) if send_ats else ("", "")
    with db.conn() as c:
        c.execute(
            "UPDATE campaign_batches SET status='sending', window_start=?, window_end=?, "
            "loaded_at=datetime('now') WHERE id=?",
            (win_start, win_end, batch_row["id"]),
        )
        c.commit()

    return {"batch_no": batch_no, "recipients": len(recips), "seeds": len(seeds),
            "packs": len(packs), "window_start": win_start, "window_end": win_end,
            "cloud_ack": ack}


async def _load_send_ctx(campaign_id: int) -> _SendCtx:
    """Rebuild the send context for approve-next (no in-memory launch state)."""
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    if not bearer_path.exists():
        raise LaunchError("cloud bearer missing")
    bearer = bearer_path.read_text().strip()
    with db.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            raise LaunchError(f"campaign {campaign_id} not found")
        camp = dict(camp)
        tmpl = dict(c.execute("SELECT * FROM templates WHERE id=?", (camp["template_id"],)).fetchone())
    public_host = settings.get("public_host", "mail.example.com") or "mail.example.com"
    entity_id = camp.get("entity_id")
    if entity_id:
        from mailchad.terminal.routes_admin import get_entity_credentials
        creds = get_entity_credentials(entity_id)
        resend_key, email_from_name, email_from = creds["resend_key"], creds["from_name"], creds["from_email"]
    else:
        resend_key = settings.get("resend_api_key", "") or ""
        email_from = settings.get("email_from", f"noreply@{public_host}") or f"noreply@{public_host}"
        email_from_name = settings.get("from_name", "") or ""
    if not resend_key:
        raise LaunchError("resend_api_key not configured")
    k_temp = await _ensure_k_temp(bearer)
    key_id = f"K_temp_{encryption.k_temp_id(k_temp)}"
    return _SendCtx(campaign_id=campaign_id, camp=camp, tmpl=tmpl, public_host=public_host,
                    resend_key=resend_key, email_from=email_from, email_from_name=email_from_name,
                    k_temp=k_temp, key_id=key_id, bearer=bearer)
