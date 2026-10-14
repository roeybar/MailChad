"""Campaign launch - terminal side, v3.1 refactor (section 4.1).

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
import re
import secrets as _secrets
import time
import uuid
from datetime import datetime, timezone
from email.utils import formataddr

import httpx
from sqlalchemy import TEXT, func, insert, literal, select, update
from sqlalchemy.orm import aliased

from mailman.terminal import db, encryption, settings, send_schedule
from mailman.terminal.models import (Campaign, CampaignBatch, CampaignRecipient, CampaignStage, Contact, DispatchedJob,
                                      Entity, SeedAddress, SuppressionHash, Template, cols)

log = logging.getLogger("terminal.launch")

CLOUD_URL = os.environ.get("CLOUD_URL", "http://cloud:8443")   # infra-bound, env-only


class LaunchError(Exception):
    pass


class RelaunchConflict(LaunchError):
    """A Re-launch press from a page rendered before the last Re-launch press (409)."""


class LaunchRetryable(LaunchError):
    """A refusal that clears with no change to the campaign (no cloud handshake yet, recipients locked by
    another running campaign). Callers see a LaunchError; the scheduler tries again next tick."""


class LaunchPushError(LaunchError):
    """The cloud refused a /packs push. Chunks pushed before it, or by an earlier try, may already be live,
    so the scheduler records a stage that fails this way as possibly sent."""


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

def _render(template: dict, contact: dict, company_name: str | None = None) -> tuple[str, str, str]:
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
    if company_name is not None:
        subject = subject.replace("{{companyName}}", company_name)
        html = html.replace("{{companyName}}", company_name)
        text = text.replace("{{companyName}}", company_name)
    return subject, html, text


_TOKEN_RE = re.compile(r"\{\{[^{}]*\}\}")


def assert_sendable(subject: str, html: str, text: str) -> None:
    """No outgoing mail carries a literal {{...}}."""
    parts = [p or "" for p in (subject, html, text)]
    if any("{{" in p for p in parts):
        found = sorted({m.group(0) for p in parts for m in _TOKEN_RE.finditer(p)}) or ["{{"]
        raise LaunchError("unfilled template variables: " + ", ".join(found)
                          + " - use only the variables listed on the Templates page")


_M6_CAP = 10


def _assert_recipients_sendable(ctx: "_SendCtx", recipients: list[dict]) -> None:
    """Every recipient and seed renders with no leftover {{...}} before any state change."""
    bad = []
    for r in recipients:
        s_, h_, t_ = _render(ctx.tmpl, r, ctx.company_name)
        h_, t_ = _inject_compliance(h_, t_, "https://example.com/u/x", "https://example.com/e/x", ctx.camp["kind"])
        found = sorted({m.group(0) for p in (s_, h_, t_) for m in _TOKEN_RE.finditer(p or "")})
        if found or any("{{" in (p or "") for p in (s_, h_, t_)):
            bad.append(f"{r['email']} ({', '.join(found) or '{{'})")
    if bad:
        more = f"; and {len(bad) - _M6_CAP} more" if len(bad) > _M6_CAP else ""
        raise LaunchError(f"{len(bad)} contact(s) cannot be sent - their data leaves template variables in the mail: "
                          + "; ".join(bad[:_M6_CAP]) + more
                          + " - fix those contacts or remove them from the campaign")


def resolve_company_name(entity_id: int | None, template: dict) -> str | None:
    """The {{companyName}} value for a send: the campaign entity's name."""
    if entity_id:
        with db.session() as s:
            name = s.execute(select(Entity.name).where(Entity.id == entity_id)).scalar_one_or_none()
        if name is None:
            raise LaunchError(f"company {entity_id} not found")
        if not name.strip():
            raise LaunchError(f"company {entity_id} has no name - set one on the Entities page before sending")
        return name.strip()
    if any("{{companyName}}" in (template.get(k) or "") for k in ("subject", "html_body", "text_body")):
        raise LaunchError("this template uses {{companyName}} but the campaign has no company"
                          " - choose a company for the campaign before sending")
    return None


def _preflight(tmpl: dict, camp: dict, entity_id: int | None) -> str | None:
    """Render once before any state change; refuse what assert_sendable refuses."""
    company_name = resolve_company_name(entity_id, tmpl)
    s_, h_, t_ = _render(tmpl, {"email": "preflight@example.com", "name": "Preflight Person"}, company_name)
    h_, t_ = _inject_compliance(h_, t_, "https://example.com/u/x", "https://example.com/e/x", camp["kind"])
    assert_sendable(s_, h_, t_)
    return company_name


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
                f"<a href='{unsub_url}' style='color:#666'>Unsubscribe</a> \u00B7 "
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
                          relaunch: bool = False,
                          expected_seq: int | None = None) -> dict:
    """Launch a campaign (or one stage of a multi-stage campaign).

    template_id_override: use a different template (for stages). Skips hash gate.
    stage_id: if set, marks that campaign_stage as dispatched on success.
    """
    if relaunch:
        _refuse_closed(campaign_id, _CLOSED_TO_ALL)       # Re-launch, before the claim and any selection
    elif stage_id is not None:
        _refuse_closed(campaign_id, _CLOSED_TO_STAGES)    # a stage
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    if not bearer_path.exists():
        raise LaunchRetryable("cloud bearer missing - handshake helper: `scripts/v3 init-handshake` (see Known limitations in the README)")
    bearer = bearer_path.read_text().strip()

    if relaunch:
        _claim_relaunch(campaign_id, expected_seq)        # before any read that decides the action
        with db.session() as s:
            interrupted = s.execute(
                select(CampaignBatch.batch_no)
                .where(CampaignBatch.campaign_id == campaign_id, CampaignBatch.dispatch_started_at.is_not(None))
                .order_by(CampaignBatch.batch_no).limit(1)).scalar_one_or_none()
        if interrupted is not None:
            return await _finish_interrupted_batch(campaign_id, interrupted, actor)

    with db.session() as s:
        camp = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise LaunchError(f"campaign {campaign_id} not found")
        camp = dict(camp)

        # For stage launches, allow dispatched/tested campaigns; bypass hash gate
        is_stage = stage_id is not None
        if not is_stage and not relaunch:
            if camp["status"] not in ("tested", "scheduled"):
                raise LaunchError(f"campaign status {camp['status']!r}; must be tested or scheduled")
            if not camp.get("test_sent_at"):
                raise LaunchError("test send required before launch")

        tmpl_id = template_id_override or camp["template_id"]
        tmpl = s.execute(select(*cols(Template)).where(Template.id == tmpl_id)).mappings().first()
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
        cr, c2 = aliased(CampaignRecipient), aliased(Contact)
        if is_stage or relaunch:
            status_filter = cr.status.in_(("queued", "sent", "opened", "clicked", "bounced", "complained", "failed"))
        else:
            status_filter = cr.status == "queued"
        recipients = [dict(r) for r in s.execute(
            select(cr.contact_id, cr.status, c2.email, c2.name)
            .join(c2, c2.id == cr.contact_id)
            .where(cr.campaign_id == campaign_id, status_filter)
            .order_by(cr.contact_id)
        ).mappings().all()]

    suppressed = _suppressed_map(camp["kind"])
    if not recipients and not (relaunch and camp["relaunch_open"]):
        raise LaunchError("no queued recipients")

    # Per-day single-campaign lock: refuse if any recipient is still locked by
    # another in-flight campaign (anti-spam + compliance + no info loss).
    if not is_stage:
        from mailman.terminal import campaign_lock
        conflicts = campaign_lock.find_conflicts(campaign_id, [r["contact_id"] for r in recipients])
        if conflicts:
            ex = conflicts[0]
            raise LaunchRetryable(
                f"{len(conflicts)} contact(s) are locked by another running campaign "
                f"(e.g. '{ex['campaign_name']}', free at {ex['free_at']} UTC). "
                f"A contact can only be on one campaign per day."
            )

    public_host = settings.get("public_host", "mail.example.com") or "mail.example.com"

    entity_id = camp.get("entity_id")
    if entity_id:
        from mailman.terminal.routes_admin import get_entity_credentials
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
    company_name = _preflight(tmpl, camp, entity_id)

    k_temp = await _ensure_k_temp(bearer)
    key_id = f"K_temp_{encryption.k_temp_id(k_temp)}"

    ctx = _SendCtx(campaign_id=campaign_id, camp=camp, tmpl=tmpl, public_host=public_host,
                   resend_key=resend_key, email_from=email_from, email_from_name=email_from_name,
                   k_temp=k_temp, key_id=key_id, bearer=bearer, company_name=company_name)

    # Legacy single-shot path: stages + relaunch (no batching/windowing)
    if is_stage or relaunch:
        send_at = _pack_send_at(camp.get("scheduled_for"))
        deliver = [r for r in recipients
                   if hashlib.sha256(r["email"].lower().encode()).hexdigest() not in suppressed]
        _assert_recipients_sendable(ctx, deliver)          # before any send state changes (a Re-launch press has already counted in relaunch_seq)
        gen = None
        if is_stage:
            gen_key, mode = f"stage:{stage_id}", "stage"
        else:
            gen, finishing = _open_relaunch(campaign_id)
            gen_key, mode = _gen_key_for(gen), ("finish-relaunch" if finishing else "relaunch")
        plan = _plan_sends(campaign_id, camp["created_at"], camp["kind"], gen_key, recipients)
        packs, jobs = [], []
        for r, pid in plan.sends:
            p, j = _build_pack(ctx, r, send_at, human_send=bool(camp.get("human_send")), pack_id=pid)
            packs.append(p); jobs.append(j)
        ack = await _push_and_record(campaign_id, packs, jobs, bearer, batch_no=None)
        with db.session() as s:
            if is_stage:
                s.execute(update(CampaignStage).where(CampaignStage.id == stage_id)
                          .values(status="dispatched", dispatched_at=func.datetime("now"))
                          .execution_options(synchronize_session=False))
            else:
                s.execute(update(Campaign).where(Campaign.id == campaign_id, Campaign.relaunch_gen == gen)
                          .values(relaunch_open=0).execution_options(synchronize_session=False))
            s.commit()
        summary = {"campaign_id": campaign_id, "dispatched": len(packs),
                   "suppressed": len(plan.suppressed), "total_recipients": len(recipients),
                   "mode": mode, "generation": gen, "cloud_ack": ack}
        if mode == "finish-relaunch":
            n = ack.get("accepted", 0)        # newly stored packs only; re-pushes the cloud already held are not counted in _NOTE_FINISH_RELAUNCH
            summary["note"] = ((_NOTE_FINISH_RELAUNCH.format(n=n) if n
                                else _NOTE_FINISH_RELAUNCH_EMPTY) + _next_action(campaign_id))
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
    _assert_recipients_sendable(ctx, deliver + [{"email": e, "name": ""} for e in _active_seed_emails()])

    batch_size = settings.get_int("send_batch_size", 1000)
    batches = [deliver[i:i + batch_size] for i in range(0, len(deliver), batch_size)]

    with db.session() as s:
        db.begin_explicit(s)
        for bi, brecips in enumerate(batches, start=1):
            s.execute(insert(CampaignBatch).prefix_with("OR IGNORE")
                      .values(campaign_id=campaign_id, batch_no=bi, size=len(brecips), status="pending")
                      .execution_options(synchronize_session=False))
            for r in brecips:
                s.execute(update(CampaignRecipient)
                          .where(CampaignRecipient.campaign_id == campaign_id,
                                 CampaignRecipient.contact_id == r["contact_id"])
                          .values(batch_no=bi).execution_options(synchronize_session=False))
        s.execute(update(Campaign).where(Campaign.id == campaign_id)
                  .values(status="dispatched", dispatched_at=func.datetime("now"),
                          lock_until=func.datetime(
                              "now", literal("+", TEXT).concat(Campaign.lock_duration_s).concat(" seconds")),
                          unsubs_confirmed_at=None, updated_at=func.datetime("now"))
                  .execution_options(synchronize_session=False))
        s.commit()

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
    company_name: str | None


def _active_seed_emails() -> list[str]:
    with db.session() as s:
        return [r["email"] for r in s.execute(
            select(SeedAddress.email).where(SeedAddress.active == 1).order_by(SeedAddress.id)
        ).mappings().all()]


def _pack_id(campaign_id: int, campaign_created_at: str, send_key: str,
             contact_id: int, ordinal: int) -> str:
    """Deterministic pack id: a retry of an unrecorded send re-derives the same id, so the
    cloud dedupes it (packs.py PackAlreadyExists). ordinal = sends of this campaign already
    recorded for the contact, so a deliberate resend after a recorded send gets a new id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"mailman:pack:v1:{campaign_id}:{campaign_created_at}:{send_key}:{contact_id}:{ordinal}"))


_SENDABLE = ("queued", "sent", "opened", "clicked", "bounced", "complained", "failed")
_RETRYABLE_JOB = ("failed", "cancelled")
_PUSHING: dict[int, int] = {}


def push_in_flight(campaign_id: int) -> bool:
    return _PUSHING.get(campaign_id, 0) > 0


def _suppressed_map(kind: str) -> dict[str, str]:
    """{email_hash: reason} for hashes that block a send of this kind. Scope-aware: promotional
    sends are blocked by promotional+all unsubs; transactional only by 'all'. Non-unsubscribe
    reasons (bounce/complaint/manual/erasure) always block."""
    with db.session() as s:
        rows = s.execute(select(SuppressionHash.email_hash, SuppressionHash.reason,
                                SuppressionHash.scope)).mappings().all()
    is_promotional = kind == "promotional"
    return {r["email_hash"]: r["reason"] for r in rows
            if r["reason"] != "unsubscribe" or r["scope"] == "all" or is_promotional}


def _gen_key_for(gen: int) -> str:
    return "main" if gen == 0 else f"relaunch:{gen}"


def _gen_key(campaign_id: int) -> str:
    """The campaign's current send generation: every batch dispatch keys on it."""
    with db.session() as s:
        gen = s.execute(select(Campaign.relaunch_gen).where(Campaign.id == campaign_id)).scalar_one()
    return _gen_key_for(gen)


@dataclasses.dataclass
class _SendPlan:
    sends: list            # [(recipient row, pack id)]
    recorded: int          # skipped: already has a send in this generation
    suppressed: list       # [(contact_id, reason)]


_PACK_ID_STUCK = ("campaign {id}: pack ids for contact {contact} kept colliding after {tries} tries,"
                  " so nothing more was sent on this press; this is a bug, report it")


def _plan_sends(campaign_id: int, created_at: str, kind: str, gen_key: str,
                candidates: list[dict]) -> _SendPlan:
    """THE send choke point. Every path that pushes contact packs derives its pack ids here
    and nowhere else, so one contact gets one id per generation: an unrecorded earlier push is
    re-derived (the cloud dedupes it, packs.py held) and a recorded one is skipped. A second
    pack in the same generation goes only to a contact the operator reset to 'queued' whose
    last pack in that generation failed or was cancelled. Suppression is applied here too."""
    blocked = _suppressed_map(kind)
    with db.session() as s:
        jobs = dict(s.execute(select(DispatchedJob.job_id, DispatchedJob.status)
                              .where(DispatchedJob.campaign_id == campaign_id)).all())
    plan = _SendPlan(sends=[], recorded=0, suppressed=[])
    for r in candidates:
        reason = blocked.get(hashlib.sha256(r["email"].lower().encode()).hexdigest())
        if reason is not None:
            plan.suppressed.append((r["contact_id"], reason))
            continue
        a = 0
        while _pack_id(campaign_id, created_at, gen_key, r["contact_id"], a) in jobs:
            a += 1
            if a > len(jobs):   # fail loudly, never spin, if ids stop varying with the ordinal
                raise LaunchError(_PACK_ID_STUCK.format(id=campaign_id, contact=r["contact_id"], tries=a))
        if a and not (r["status"] == "queued"
                      and jobs[_pack_id(campaign_id, created_at, gen_key, r["contact_id"], a - 1)] in _RETRYABLE_JOB):
            plan.recorded += 1
            continue
        plan.sends.append((r, _pack_id(campaign_id, created_at, gen_key, r["contact_id"], a)))
    return plan


class CampaignClosed(LaunchRetryable):
    """The campaign is archived (or, for a stage, cancelled). Callers see a LaunchError (a 400 with
    the reason); the scheduler leaves the stage pending, and its next pass cancels it with the reason."""


_CLOSED_REASON = {"archived": "campaign {id} is archived, and an archived campaign sends no mail. Clone it to send it again.",
                  "cancelled": "campaign {id} is cancelled, and a cancelled campaign's stages send no mail"}
_CLOSED_TO_ALL = ("archived",)                   # Re-launch, Approve & send, the batch finish
_CLOSED_TO_STAGES = ("archived", "cancelled")    # stages


def _refuse_closed(campaign_id: int, closed: tuple) -> None:
    """The first check on every path that reaches _plan_sends, before any claim, selection, marker
    or push. A missing campaign passes here and is refused later with the standard not-found message."""
    with db.session() as s:
        status = s.execute(select(Campaign.status).where(Campaign.id == campaign_id)).scalar_one_or_none()
    if status in closed:
        raise CampaignClosed(_CLOSED_REASON[status].format(id=campaign_id))


def _claim_relaunch(campaign_id: int, expected_seq: int | None) -> None:
    """Every Re-launch press bumps relaunch_seq before deciding anything. A press carrying the
    seq its page showed bumps it only if it still matches, in the same UPDATE, so a second press
    from a stale page gets a 409 instead of finishing or resending."""
    with db.session() as s:
        q = update(Campaign).where(Campaign.id == campaign_id)
        if expected_seq is not None:
            q = q.where(Campaign.relaunch_seq == expected_seq)
        claimed = s.execute(q.values(relaunch_seq=Campaign.relaunch_seq + 1)
                            .execution_options(synchronize_session=False)).rowcount
        exists = s.execute(select(Campaign.id).where(Campaign.id == campaign_id)).first() is not None
        s.commit()
    if not claimed and exists:
        raise RelaunchConflict(_STALE_RELAUNCH)


def _open_relaunch(campaign_id: int) -> tuple[int, bool]:
    """(generation, finishing). One conditional UPDATE decides, so two presses never open two
    generations: a press while relaunch_open=1 finishes the open one."""
    with db.session() as s:
        opened = s.execute(update(Campaign).where(Campaign.id == campaign_id, Campaign.relaunch_open == 0)
                           .values(relaunch_gen=Campaign.relaunch_gen + 1, relaunch_open=1)
                           .execution_options(synchronize_session=False)).rowcount
        gen = s.execute(select(Campaign.relaunch_gen).where(Campaign.id == campaign_id)).scalar_one()
        s.commit()
    return gen, not opened


def _next_action(campaign_id: int) -> str:
    with db.session() as s:
        b = s.execute(select(CampaignBatch.batch_no)
                      .where(CampaignBatch.campaign_id == campaign_id,
                             CampaignBatch.dispatch_started_at.is_not(None))
                      .order_by(CampaignBatch.batch_no).limit(1)).scalar_one_or_none()
        is_open = s.execute(select(Campaign.relaunch_open).where(Campaign.id == campaign_id)).scalar_one()
    if b is not None:
        return " " + _NEXT_FINISH_BATCH.format(batch_no=b)
    return " " + (_NEXT_FINISH_RELAUNCH if is_open else _NEXT_DONE)


def _mark_suppressed(campaign_id: int, rows: list) -> None:
    """A queued contact filtered at dispatch is closed, with the reason."""
    with db.session() as s:
        for contact_id, reason in rows:
            s.execute(update(CampaignRecipient)
                      .where(CampaignRecipient.campaign_id == campaign_id,
                             CampaignRecipient.contact_id == contact_id)
                      .values(status="cancelled", failure_reason=f"suppressed: {reason}")
                      .execution_options(synchronize_session=False))
        s.commit()


def _mark_batch_loaded(batch_id: int, win_start: str, win_end: str) -> None:
    with db.session() as s:
        s.execute(update(CampaignBatch).where(CampaignBatch.id == batch_id)
                  .values(status="sending", window_start=win_start, window_end=win_end,
                          loaded_at=func.datetime("now"), dispatch_started_at=None)
                  .execution_options(synchronize_session=False))
        s.commit()


def _close_empty_batch(batch_id: int, batch_no: int, *, handed_over: bool, suppressed: int) -> dict:
    """Nothing left to push for this batch in this generation. handed_over: some of it
    reached the cloud (marker set, or members already sent): let it drain and cool down.
    Otherwise (all suppressed) close it at once."""
    now_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if handed_over:
        values, win = dict(status="sending", window_start=now_z, window_end=now_z,
                           loaded_at=func.datetime("now")), now_z
    else:
        values, win = dict(status="drained", drained_at=now_z, approve_unlock_at=None,
                           window_start="", window_end=""), ""
    with db.session() as s:
        s.execute(update(CampaignBatch).where(CampaignBatch.id == batch_id)
                  .values(dispatch_started_at=None, **values)
                  .execution_options(synchronize_session=False))
        s.commit()
    return {"batch_no": batch_no, "recipients": 0, "suppressed": suppressed, "seeds": 0, "packs": 0,
            "window_start": win, "window_end": win, "cloud_ack": None}


def _from_header(name: str | None, address: str) -> str:
    """The From a recipient sees: "Name <address>" when the sender has a name, else the bare address.
    Whitespace runs (CR and LF included) collapse to one space, so a stored name cannot add a header."""
    name = " ".join((name or "").split())
    return formataddr((name, address)) if name else address


_SEND_NOW = "1970-01-01T00:00:00Z"


def _pack_send_at(scheduled_for: str | None) -> str:
    """A pack send_at in the cloud's text-comparable form "YYYY-MM-DDTHH:MM:SSZ" (cloud
    dynamo._now, send_schedule). An empty or unparseable stored time means "send now"."""
    from mailman.terminal.routes_admin import parse_utc_naive
    if not (scheduled_for or "").strip():
        return _SEND_NOW
    try:
        when = parse_utc_naive(scheduled_for)
    except ValueError:
        log.warning("launch: scheduled_for %r is not a valid time; packs are sent now", scheduled_for)
        return _SEND_NOW
    return when.isoformat(timespec="seconds") + "Z"


def _build_pack(ctx: _SendCtx, r: dict, send_at: str, *, human_send: bool, is_seed: bool = False, pack_id: str | None = None) -> tuple[dict, dict]:
    """Build one wire pack + its dispatched_job record for recipient r at send_at.
    pack_id: from _pack_id for contacts; None (seeds) draws a random id."""
    camp, tmpl, campaign_id = ctx.camp, ctx.tmpl, ctx.campaign_id
    email = r["email"]
    email_hash = hashlib.sha256(email.lower().encode()).hexdigest()
    subject, html, text = _render(tmpl, r, ctx.company_name)
    unsub_ttl   = settings.get_int("unsub_token_ttl_s",   60 * 60 * 24 * 365)
    erasure_ttl = settings.get_int("erasure_token_ttl_s", 60 * 60 * 24 * 30)
    unsub_token = mint_token("unsub", email_hash, unsub_ttl)
    erasure_token = mint_token("erasure", email_hash, erasure_ttl)
    unsub_url = f"https://{ctx.public_host}/u/{unsub_token}"
    erasure_url = f"https://{ctx.public_host}/e/{erasure_token}"
    html, text = _inject_compliance(html, text, unsub_url, erasure_url, camp["kind"])
    assert_sendable(subject, html, text)

    plain_pack = {
        "recipient": email, "subject": subject, "html": html, "text": text,
        "headers": _list_unsub_headers(unsub_url), "from": _from_header(ctx.email_from_name, ctx.email_from),
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
    pack_id = pack_id or str(uuid.uuid4())
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


class _PushInterrupted(Exception):
    """_push_packs stopped after `pushed` packs were acknowledged; the caller records
    those, then re-raises `error` (the exception callers see)."""
    def __init__(self, error: BaseException, pushed: int, held: list[dict]):
        super().__init__(str(error))
        self.error, self.pushed, self.held = error, pushed, held


_RESOLVED_PACK = ("sent", "failed", "stuck_no_key", "cancelled", "unknown")   # dynamo.RESOLVED_PACK_STATUSES

_NOTE_FINISH_BATCH = "Finished the interrupted send of batch {batch_no}: {n} recipient(s) still waiting are now scheduled in the send window; no contact who already had it was mailed again, though seed addresses may get a repeat copy."
_NOTE_FINISH_BATCH_EMPTY = "Batch {batch_no} had already been handed over for sending; its status is now up to date. Nobody was mailed again."
_NOTE_FINISH_RELAUNCH = "Finished the interrupted re-launch: {n} recipient(s) who had not been sent it are now queued for sending, and nobody who already had it was mailed again."
_NOTE_FINISH_RELAUNCH_EMPTY = "The interrupted re-launch had nothing left to send: everyone due it had already been handed it or is suppressed. It is now marked complete. Nobody was mailed again."
_NEXT_FINISH_BATCH = "Batch {batch_no} was also interrupted: press Re-launch again to finish it."
_NEXT_FINISH_RELAUNCH = "A re-launch is still unfinished: press Re-launch again to finish it."
_NEXT_DONE = "No interrupted send is left. Pressing Re-launch again sends everyone the mail again."
_STALE_RELAUNCH = "Re-launch was pressed for this campaign after this page was loaded, so this press was not carried out. The page will reload: check the campaign's status before pressing Re-launch again."
_CONCURRENT_PRESS = "Another press is already sending this campaign's mail, so this press was not carried out. Reload the page and check the campaign before pressing again."
def _concurrent_press_detail(where: str) -> str:
    """Log an integrity error answered as a concurrent press, with a short reference the 409 also carries.
    Call it only inside the except clause, so exc_info picks up the error."""
    ref = uuid.uuid4().hex[:8]
    log.warning("%s: integrity error answered as a concurrent press (ref %s)", where, ref, exc_info=True)
    return f"{_CONCURRENT_PRESS} (ref {ref})"


_NOTE_FINISH_BATCH_SUPPRESSED = "Batch {batch_no} had nobody left to send to: every recipient still waiting is on the suppression list, so this press mailed nobody."


PACK_PUSH_CHUNK = int(os.environ.get("PACK_PUSH_CHUNK", "100"))


async def _push_packs(packs: list[dict], bearer: str) -> dict:
    """Push packs to the cloud in chunks - each pack carries an encrypted HTML payload
    (~12KB), so a full 1000-pack batch is ~10MB+ and exceeds API Gateway's request-size
    limit (413). Chunk at PACK_PUSH_CHUNK and aggregate the acks.
    Ack key `held` lists packs the cloud already had (a retry); callers pop it."""
    agg = {"accepted": 0, "submitted": 0, "enqueued": 0, "deferred": 0, "held": []}
    if not packs:
        return agg
    pushed = 0
    async with httpx.AsyncClient(timeout=120) as client:
        for i in range(0, len(packs), PACK_PUSH_CHUNK):
            part = packs[i:i + PACK_PUSH_CHUNK]
            try:
                resp = await client.post(f"{CLOUD_URL}/packs", json=part,
                                         headers={"Authorization": f"Bearer {bearer}"})
                if resp.status_code >= 400:
                    raise LaunchPushError(f"cloud /packs rejected ({resp.status_code}) on chunk "
                                      f"{i // PACK_PUSH_CHUNK + 1}: {resp.text[:160]}")
                j = resp.json()
            except Exception as e:
                if pushed:
                    raise _PushInterrupted(e, pushed, agg["held"])
                raise
            pushed += len(part)
            for k in ("accepted", "submitted", "enqueued", "deferred"):
                agg[k] += j.get(k, 0)
            agg["held"].extend(j.get("held", []))
    return agg


def _record_dispatch(campaign_id: int, jobs: list[dict], batch_no: int | None) -> None:
    """Persist dispatched_job + mark recipients sent. Seeds (no contact_id) are
    fire-and-forget - no recipient/job row (tracked via seed_placements)."""
    with db.session() as s:
        db.begin_explicit(s)
        for j in jobs:
            if j.get("is_seed") or j.get("contact_id") is None:
                continue
            s.execute(
                insert(DispatchedJob)
                .values(job_id=j["pack_id"], campaign_id=campaign_id, recipient_id=j["contact_id"],
                        recipient_hash=j["recipient_hash"], content_hash=j["content_hash"],
                        dispatched_at=func.datetime("now"))
                .execution_options(synchronize_session=False)
            )
            s.execute(
                update(CampaignRecipient)
                .where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.contact_id == j["contact_id"])
                .values(status="sent", sent_at=func.datetime("now"))
                .execution_options(synchronize_session=False)
            )
        s.commit()


def _apply_held_outcomes(held: list[dict]) -> list[str]:
    """Packs the cloud already held under these ids (an earlier attempt pushed them). A
    resolved one gets its outcome applied to the job just recorded, so the recipient shows
    what really happened; pending/claimed ones resolve through the outcomes loop.
    One failure does not stop the rest; returns 'id (Error)' per failure."""
    failed: list[str] = []
    resolved = [p for p in held if p.get("status") in _RESOLVED_PACK]
    if not resolved:
        return failed
    from mailman.terminal import sync_client
    for p in resolved:
        try:
            sync_client._apply_pack_outcome(p)
        except Exception as exc:
            failed.append(f"{p.get('pack_id')} ({type(exc).__name__})")
    return failed


async def _push_and_record(campaign_id: int, packs: list[dict], jobs: list[dict],
                           bearer: str, batch_no: int | None) -> dict:
    """Push, then record. On a failure part-way, the chunks the cloud acknowledged are
    recorded before the error propagates. Unrecorded contact recipients keep their
    deterministic ids, so the cloud deduplicates a retry before local recording.
    Seed recipients get fresh ids on retry and can be sent again."""
    _PUSHING[campaign_id] = _PUSHING.get(campaign_id, 0) + 1
    try:
        try:
            ack = await _push_packs(packs, bearer)
        except _PushInterrupted as exc:
            try:
                _record_dispatch(campaign_id, jobs[:exc.pushed], batch_no=batch_no)
            except Exception as rec:
                log.error("campaign %s: %d acknowledged pack(s) left unrecorded after a failed push (%s); "
                          "a retry records them", campaign_id, exc.pushed, type(rec).__name__)
            else:
                try:
                    failed = _apply_held_outcomes(exc.held)
                except Exception as outcome:
                    failed = [f"{p.get('pack_id')} ({type(outcome).__name__})" for p in exc.held]
                if failed:
                    log.error("campaign %s: acknowledged pack(s) recorded, but applying held outcomes failed "
                              "for pack ids %s", campaign_id, failed)
            raise exc.error
        _record_dispatch(campaign_id, jobs, batch_no=batch_no)
        held = ack.pop("held", [])
        try:
            failed = _apply_held_outcomes(held)
        except Exception as outcome:
            failed = [f"{p.get('pack_id')} ({type(outcome).__name__})" for p in held]
        if failed:
            log.error("campaign %s: pack(s) recorded, but applying held outcomes failed "
                      "for pack ids %s", campaign_id, failed)
        return ack
    finally:
        _PUSHING[campaign_id] -= 1
        if not _PUSHING[campaign_id]:
            _PUSHING.pop(campaign_id)


async def _finish_interrupted_batch(campaign_id: int, batch_no: int, actor: str) -> dict:
    """Re-launch while a batch dispatch is interrupted finishes that batch through dispatch_batch
    (same generation, same ids, so the cloud dedupes; same pacing) instead of resending."""
    res = await dispatch_batch(campaign_id, batch_no)
    if batch_no > 1:      # the press completes an interrupted approve: keep approve's bookkeeping
        with db.session() as s:
            s.execute(update(CampaignBatch)
                      .where(CampaignBatch.campaign_id == campaign_id, CampaignBatch.batch_no == batch_no)
                      .values(approved_at=func.datetime("now")).execution_options(synchronize_session=False))
            s.execute(update(CampaignBatch)
                      .where(CampaignBatch.campaign_id == campaign_id, CampaignBatch.batch_no == batch_no - 1)
                      .values(status="done").execution_options(synchronize_session=False))
            s.commit()
    ack = res["cloud_ack"] or {}
    n = max(0, ack.get("accepted", 0) - res["seeds"])     # newly handed over; seeds are always new (uuid4)
    if n:
        note = _NOTE_FINISH_BATCH.format(batch_no=batch_no, n=n)
    elif "recorded" in res and res["suppressed"] and not res["recorded"]:   # empty-batch result only: nothing of it was recorded as handed over
        note = _NOTE_FINISH_BATCH_SUPPRESSED.format(batch_no=batch_no)
    else:
        note = _NOTE_FINISH_BATCH_EMPTY.format(batch_no=batch_no)
    note += _next_action(campaign_id)
    summary = {"campaign_id": campaign_id, "mode": "finish-batch", "batch_no": batch_no,
               "dispatched": res["recipients"], "total_recipients": res["recipients"], "suppressed": res["suppressed"],
               "note": note, "cloud_ack": res["cloud_ack"]}
    db.audit(actor=actor, action="campaign.launched", target=f"campaign:{campaign_id}", details=summary)
    log.info("launched %s (finish-batch %s): %s", campaign_id, batch_no, summary)
    return summary


async def dispatch_batch(campaign_id: int, batch_no: int, ctx: _SendCtx | None = None) -> dict:
    """Compile + load one batch: compute a send_at per recipient (window + senders +
    rush), salt the batch with active seeds, push all packs, and mark the batch
    sending. Self-contained so approve-next can call it for batch K+1. The cloud
    fires each pack when its send_at comes due - terminal uninvolved thereafter."""
    _refuse_closed(campaign_id, _CLOSED_TO_ALL)           # approve, the batch finish, launch's batch 1
    if ctx is None:
        ctx = await _load_send_ctx(campaign_id)

    with db.session() as s:
        cr, c2 = aliased(CampaignRecipient), aliased(Contact)
        recips = [dict(r) for r in s.execute(
            select(cr.contact_id, cr.status, c2.email, c2.name)
            .join(c2, c2.id == cr.contact_id)
            .where(cr.campaign_id == campaign_id, cr.batch_no == batch_no, cr.status == "queued")
            .order_by(cr.contact_id)
        ).mappings().all()]
        batch_row = s.execute(
            select(CampaignBatch.id, CampaignBatch.dispatch_started_at)
            .where(CampaignBatch.campaign_id == campaign_id, CampaignBatch.batch_no == batch_no)
        ).mappings().first()
    marker = batch_row["dispatch_started_at"] if batch_row else None
    if not recips and marker is None:
        raise LaunchError(f"batch {batch_no}: no queued recipients")

    plan = _plan_sends(campaign_id, ctx.camp["created_at"], ctx.camp["kind"], _gen_key(campaign_id), recips)
    seeds = [{"email": e, "name": "", "contact_id": None} for e in _active_seed_emails()]
    _assert_recipients_sendable(ctx, [r for r, _ in plan.sends] + seeds)
    if plan.suppressed:
        _mark_suppressed(campaign_id, plan.suppressed)
    if not plan.sends:
        out = _close_empty_batch(batch_row["id"], batch_no,
                                 handed_over=bool(plan.recorded or marker), suppressed=len(plan.suppressed))
        out["recorded"] = plan.recorded
        return out
    recips = [r for r, _ in plan.sends]
    pids = {r["contact_id"]: pid for r, pid in plan.sends}
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
        p, j = _build_pack(ctx, r, send_at, human_send=False, is_seed=is_seed,
                           pack_id=None if is_seed else pids[r["contact_id"]])
        packs.append(p); jobs.append(j)

    with db.session() as s:          # after every pre-push check: an interrupted push leaves this set
        s.execute(update(CampaignBatch).where(CampaignBatch.id == batch_row["id"])
                  .values(dispatch_started_at=func.datetime("now"))
                  .execution_options(synchronize_session=False))
        s.commit()
    ack = await _push_and_record(campaign_id, packs, jobs, ctx.bearer, batch_no=batch_no)

    win_start, win_end = (send_ats[0], send_ats[-1]) if send_ats else ("", "")
    _mark_batch_loaded(batch_row["id"], win_start, win_end)
    return {"batch_no": batch_no, "recipients": len(recips), "suppressed": len(plan.suppressed),
            "seeds": len(seeds), "packs": len(packs), "window_start": win_start, "window_end": win_end,
            "cloud_ack": ack}


async def _load_send_ctx(campaign_id: int) -> _SendCtx:
    """Rebuild the send context for approve-next (no in-memory launch state)."""
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    if not bearer_path.exists():
        raise LaunchError("cloud bearer missing")
    bearer = bearer_path.read_text().strip()
    with db.session() as s:
        camp = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise LaunchError(f"campaign {campaign_id} not found")
        camp = dict(camp)
        tmpl = dict(s.execute(select(*cols(Template)).where(Template.id == camp["template_id"])).mappings().first())
    public_host = settings.get("public_host", "mail.example.com") or "mail.example.com"
    entity_id = camp.get("entity_id")
    if entity_id:
        from mailman.terminal.routes_admin import get_entity_credentials
        try:
            creds = get_entity_credentials(entity_id)
        except ValueError as e:
            raise LaunchError(str(e))
        resend_key, email_from_name, email_from = creds["resend_key"], creds["from_name"], creds["from_email"]
    else:
        resend_key = settings.get("resend_api_key", "") or ""
        email_from = settings.get("email_from", f"noreply@{public_host}") or f"noreply@{public_host}"
        email_from_name = settings.get("from_name", "") or ""
    if not resend_key:
        raise LaunchError("resend_api_key not configured")
    company_name = _preflight(tmpl, camp, entity_id)
    k_temp = await _ensure_k_temp(bearer)
    key_id = f"K_temp_{encryption.k_temp_id(k_temp)}"
    return _SendCtx(campaign_id=campaign_id, camp=camp, tmpl=tmpl, public_host=public_host,
                    resend_key=resend_key, email_from=email_from, email_from_name=email_from_name,
                    k_temp=k_temp, key_id=key_id, bearer=bearer, company_name=company_name)


def build_test_mail(camp: dict, tmpl: dict, to_email: str) -> tuple[str, dict]:
    """The campaign Test action's mail. Refuses what a launch refuses, with the launch's messages
    (footer rule, sender and key, template checks, and the launch's own recipients and seeds),
    then builds the pack a launch would build for to_email with _build_pack and returns
    (resend key, the Resend body the dispatcher would post, subject prefixed "[Test] ").
    Nothing is stored or pushed; the K_temp below is throwaway and only opens the pack."""
    if camp["kind"] == "promotional" and not settings.get("email_footer_address"):
        raise LaunchError("promotional needs email_footer_address setting (CAN-SPAM)")
    public_host = settings.get("public_host", "mail.example.com") or "mail.example.com"
    entity_id = camp.get("entity_id")
    if entity_id:
        from mailman.terminal.routes_admin import get_entity_credentials
        try:
            creds = get_entity_credentials(entity_id)
        except ValueError as e:
            raise LaunchError(str(e))
        resend_key = creds["resend_key"]
        email_from_name = creds["from_name"]
        email_from = creds["from_email"]
    else:
        resend_key = settings.get("resend_api_key", "") or ""
        email_from = settings.get("email_from", f"noreply@{public_host}") or f"noreply@{public_host}"
        email_from_name = settings.get("from_name", "") or ""
    if not resend_key:
        raise LaunchError("resend_api_key not configured - set via admin UI or on the company")
    company_name = _preflight(tmpl, camp, entity_id)
    k = encryption.mint_k_temp()
    ctx = _SendCtx(campaign_id=camp["id"], camp=camp, tmpl=tmpl, public_host=public_host,
                   resend_key=resend_key, email_from=email_from, email_from_name=email_from_name,
                   k_temp=k, key_id="", bearer="", company_name=company_name)
    with db.session() as s:
        recipients = [dict(r) for r in s.execute(
            select(CampaignRecipient.contact_id, CampaignRecipient.status, Contact.email, Contact.name)
            .join(Contact, Contact.id == CampaignRecipient.contact_id)
            .where(CampaignRecipient.campaign_id == camp["id"], CampaignRecipient.status == "queued")
            .order_by(CampaignRecipient.contact_id)).mappings().all()]
        known = s.execute(select(Contact.name).where(func.lower(Contact.email) == to_email.lower())
                          .order_by(Contact.id).limit(1)).scalar_one_or_none()
    suppressed = _suppressed_map(camp["kind"])
    deliver = [r for r in recipients
               if hashlib.sha256(r["email"].lower().encode()).hexdigest() not in suppressed]
    _assert_recipients_sendable(ctx, deliver + [{"email": e, "name": ""} for e in _active_seed_emails()])
    pack, _ = _build_pack(ctx, {"email": to_email, "name": known or ""}, "1970-01-01T00:00:00Z",
                          human_send=False, is_seed=True)
    plain = json.loads(encryption.decrypt_with_temp(base64.b64decode(pack["encrypted_blob"]), k))
    body = {"from": plain["from"], "to": [plain["recipient"]], "subject": "[Test] " + plain["subject"],
            "html": plain["html"], "headers": plain.get("headers", {}),
            "open_tracking": False, "click_tracking": True}
    if plain.get("text"):
        body["text"] = plain["text"]
    return plain["resend_api_key"], body
