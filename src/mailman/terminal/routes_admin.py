"""Admin CRUD endpoints - vault-side.

Ported in spirit from email-platform-v2/app/routes/internal.py + the v1.7.6
Next.js admin actions.

Coverage:
  Contacts:   list / get / create / update / delete
  Templates:  list / get / create / update / delete (template_hash auto)
  Campaigns:  list / get / create / add-recipients / mark-tested / launch
  Suppression: manual add / list (hashes only)
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import aliased

from mailman.terminal import db, settings as _settings
from mailman.terminal.auth import require_session
from mailman.terminal.models import (Campaign, CampaignBatch, CampaignRecipient, CampaignStage, Contact, Entity,
                                      Operator, SeedAddress, SeedPlacement, SuppressionHash, Template, cols)

log = logging.getLogger("vault.admin")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_session)])


def _require_admin(session: dict = Depends(require_session)) -> dict:
    """Route dependency: the operator-account routes are for admins only, as on the HTML pages."""
    if session.get("role", "admin") != "admin":
        raise HTTPException(403, "admin role required")
    return session


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()


def _template_hash(subject: str, from_name: str, html_body: str) -> str:
    """Stable hash over recipient-visible content. Drives the v1.7.6 launch gate."""
    return hashlib.sha256(f"{subject}\n{from_name}\n{html_body}".encode("utf-8")).hexdigest()


# Contacts

class ContactIn(BaseModel):
    email: str
    name: str | None = None
    tags: str | None = None
    consent_source: str = "manual"
    external_id: str | None = None


@router.get("/contacts")
def list_contacts(limit: int = 100, offset: int = 0, tag: str | None = None):
    with db.session() as s:
        if tag:
            rows = [dict(r) for r in s.execute(
                select(*cols(Contact)).where(Contact.tags.like(f"%{tag}%"))
                .order_by(Contact.created_at.desc()).limit(limit).offset(offset)
            ).mappings().all()]
            total = s.execute(
                select(func.count().label("n")).select_from(Contact).where(Contact.tags.like(f"%{tag}%"))
            ).mappings().first()["n"]
        else:
            rows = [dict(r) for r in s.execute(
                select(*cols(Contact)).order_by(Contact.created_at.desc()).limit(limit).offset(offset)
            ).mappings().all()]
            total = s.execute(select(func.count().label("n")).select_from(Contact)).mappings().first()["n"]
    return {"contacts": rows, "total": total}


@router.get("/contacts/count")
def count_contacts(tag: str | None = None):
    with db.session() as s:
        if tag:
            n = s.execute(
                select(func.count().label("n")).select_from(Contact).where(Contact.tags.like(f"%{tag}%"))
            ).mappings().first()["n"]
        else:
            n = s.execute(select(func.count().label("n")).select_from(Contact)).mappings().first()["n"]
    return {"count": n, "tag": tag}


@router.post("/contacts")
def create_contact(c_in: ContactIn):
    email = c_in.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "invalid email")

    h = _email_hash(email)
    with db.session() as s:
        # Refuse insert if hash is in suppression.
        sup = s.execute(
            select(SuppressionHash.reason).where(SuppressionHash.email_hash == h)
        ).mappings().first()
        if sup:
            raise HTTPException(409, f"email is in suppression (reason={sup['reason']})")
        try:
            res = s.execute(
                insert(Contact)
                .values(email=email, name=c_in.name, tags=c_in.tags, consent_ts=_now(),
                        consent_source=c_in.consent_source, external_id=c_in.external_id)
                .execution_options(synchronize_session=False)
            )
            s.commit()
            new_id = res.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "email already exists")
            raise
    db.audit(actor="operator", action="contact.created", target=f"contact:{new_id}",
             details={"email_hash": h})
    return {"id": new_id, "email": email}


MAX_CSV_ROWS = 10_000  # per-request chunk limit; client splits larger files


def _rows_from_xlsx(raw: bytes) -> list[list[str]]:
    import openpyxl, io as _io
    wb = openpyxl.load_workbook(_io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    out = []
    for row in ws.iter_rows(values_only=True):
        out.append([str(cell) if cell is not None else "" for cell in row])
    wb.close()
    return out


@router.post("/contacts/csv")
async def csv_import(file: UploadFile = File(...), batch_tag: str = Form("")):
    """Bulk-import contacts from a CSV or XLSX chunk. Max 10,000 rows per request.

    batch_tag: optional tag applied to every successfully imported contact,
               merged with any tags already present in the CSV row.
    """
    raw = await file.read()
    fname = (file.filename or "").lower()

    if fname.endswith(".xlsx") or file.content_type in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ):
        try:
            rows = _rows_from_xlsx(raw)
        except Exception as e:
            raise HTTPException(400, f"could not read xlsx: {e}")
    else:
        text = raw.decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))

    if len(rows) > MAX_CSV_ROWS + 1:
        raise HTTPException(413, f"chunk exceeds {MAX_CSV_ROWS} rows")

    # Detect header: first row's first cell doesn't contain '@'
    if rows and "@" not in (rows[0][0] if rows[0] else ""):
        header = [h.strip().lower() for h in rows[0]]
        data_rows = rows[1:]
    else:
        header = ["email", "name", "tags", "consent_source", "external_id"]
        data_rows = rows

    def _col(row: list, name: str, default: str = "") -> str:
        try:
            idx = header.index(name)
            return row[idx].strip() if idx < len(row) else default
        except ValueError:
            return default

    imported = skipped_suppressed = skipped_invalid = 0
    errors: list[str] = []
    dropped_reasons: dict[str, int] = {}   # cleaner gate breakdown

    from mailman.terminal import email_clean
    now = _now()
    with db.session() as s:
        for i, row in enumerate(data_rows):
            if not row:
                continue
            # Cleaner gate: drop bad syntax / role / disposable before import
            email, reason = email_clean.clean_email(_col(row, "email"))
            if reason:
                skipped_invalid += 1
                dropped_reasons[reason] = dropped_reasons.get(reason, 0) + 1
                if len(errors) < 20:
                    errors.append(f"row {i+2}: {reason} - {_col(row, 'email')!r}")
                continue
            h = _email_hash(email)
            if s.execute(select(1).select_from(SuppressionHash).where(SuppressionHash.email_hash == h)).first():
                skipped_suppressed += 1
                continue
            try:
                csv_tags = _col(row, "tags") or ""
                if batch_tag:
                    existing = [t.strip() for t in csv_tags.split("|") if t.strip()]
                    bt = batch_tag.strip()
                    if bt and bt not in existing:
                        existing.append(bt)
                    csv_tags = "|".join(existing)
                res = s.execute(
                    insert(Contact).prefix_with("OR IGNORE")
                    .values(email=email,
                            name=_col(row, "name") or None,
                            tags=csv_tags or None,
                            consent_ts=now,
                            consent_source=_col(row, "consent_source", "csv_import"),
                            external_id=_col(row, "external_id") or None)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount:
                    imported += 1
            except Exception as exc:
                if len(errors) < 20:
                    errors.append(f"row {i+2}: {exc}")
        s.commit()

    db.audit(actor="operator", action="contacts.csv_import",
             details={"imported": imported, "skipped_suppressed": skipped_suppressed,
                      "skipped_invalid": skipped_invalid, "dropped_reasons": dropped_reasons})
    return {"imported": imported, "skipped_suppressed": skipped_suppressed,
            "skipped_invalid": skipped_invalid, "dropped_reasons": dropped_reasons,
            "errors": errors}


@router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: int):
    with db.session() as s:
        row = s.execute(select(Contact.email).where(Contact.id == contact_id)).mappings().first()
        if not row:
            raise HTTPException(404, "no such contact")
        s.execute(delete(Contact).where(Contact.id == contact_id).execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="contact.deleted", target=f"contact:{contact_id}",
             details={"email_hash": _email_hash(row["email"])})
    return {"deleted": True}


@router.post("/contacts/bulk-delete")
def bulk_delete_contacts(body: dict):
    ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "no ids provided")
    deleted = 0
    with db.session() as s:
        for cid in ids:
            r = s.execute(delete(Contact).where(Contact.id == cid).execution_options(synchronize_session=False))
            deleted += r.rowcount
        s.commit()
    db.audit(actor="operator", action="contacts.bulk_deleted", details={"count": deleted})
    return {"deleted": deleted}


@router.get("/contacts/export")
def export_contacts(ids: str | None = None):
    from fastapi.responses import Response
    def _q(v): return '"' + str(v or "").replace('"', '""') + '"'
    with db.session() as s:
        if ids:
            id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
            rows = s.execute(
                select(Contact.email, Contact.name, Contact.tags, Contact.consent_source, Contact.external_id,
                       Contact.created_at)
                .where(Contact.id.in_(id_list)).order_by(Contact.id)
            ).mappings().all() if id_list else []
        else:
            rows = s.execute(
                select(Contact.email, Contact.name, Contact.tags, Contact.consent_source, Contact.external_id,
                       Contact.created_at)
                .order_by(Contact.id)
            ).mappings().all()
    lines = ["email,name,tags,consent_source,external_id,created_at"]
    for r in rows:
        lines.append(",".join(_q(r[k]) for k in ["email","name","tags","consent_source","external_id","created_at"]))
    fname = "contacts-selected.csv" if ids else "contacts.csv"
    return Response("\n".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})



# Templates

class TemplateIn(BaseModel):
    name: str
    subject: str
    from_name: str
    html_body: str
    text_body: str | None = None
    tracking_enabled: bool = True


@router.get("/templates")
def list_templates():
    with db.session() as s:
        rows = [dict(r) for r in s.execute(
            select(*cols(Template)).order_by(Template.updated_at.desc())
        ).mappings().all()]
    return {"templates": rows}


@router.post("/templates")
def create_template(t: TemplateIn):
    h = _template_hash(t.subject, t.from_name, t.html_body)
    with db.session() as s:
        try:
            res = s.execute(
                insert(Template)
                .values(name=t.name, subject=t.subject, from_name=t.from_name, html_body=t.html_body,
                        text_body=t.text_body, template_hash=h, tracking_enabled=int(t.tracking_enabled))
                .execution_options(synchronize_session=False)
            )
            s.commit()
            new_id = res.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "template name already exists")
            raise
    db.audit(actor="operator", action="template.created", target=f"template:{new_id}")
    return {"id": new_id, "template_hash": h}


@router.patch("/templates/{template_id}")
def update_template(template_id: int, t: TemplateIn):
    h = _template_hash(t.subject, t.from_name, t.html_body)
    with db.session() as s:
        row = s.execute(select(Template.id).where(Template.id == template_id)).first()
        if not row:
            raise HTTPException(404, "no such template")
        s.execute(
            update(Template).where(Template.id == template_id)
            .values(name=t.name, subject=t.subject, from_name=t.from_name, html_body=t.html_body,
                    text_body=t.text_body, template_hash=h, tracking_enabled=int(t.tracking_enabled),
                    updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="template.updated", target=f"template:{template_id}",
             details={"new_hash": h})
    return {"updated": True, "template_hash": h}


# Campaigns

_SCHEDULED_FOR_INVALID = ("scheduled_for is not a valid date and time. Use ISO 8601, for example "
                          "2026-10-12T09:00:00Z; a time without a zone is read as UTC.")


def _scheduled_for_or_400(value: str | None) -> str:
    """A posted scheduled_for in the stored UTC form, utc_text(parse_utc_naive(v)).
    Empty or invalid answers 400; a past time is accepted and is due at the next tick."""
    try:
        return utc_text(parse_utc_naive(value or ""))
    except ValueError:
        raise HTTPException(400, _SCHEDULED_FOR_INVALID)


class CampaignIn(BaseModel):
    name: str
    template_id: int
    kind: str = "promotional"   # or "transactional"
    entity_id: int | None = None
    scheduled_for: str | None = None       # ISO datetime; if set, status='scheduled'
    rate_limit_per_min: int | None = None  # None = unlimited
    bounce_pause_pct: float = 0.10         # 0.10 = pause/halve when 10% bounce
    human_send: bool = False               # randomise interval between min/max
    human_send_min_s: int = 60            # 1 min
    human_send_max_s: int = 210           # 3.5 mins
    human_send_count: int = 1             # parallel senders (N emails per sleep cycle)


@router.get("/campaigns")
def list_campaigns():
    with db.session() as s:
        rows = [dict(r) for r in s.execute(
            select(*cols(Campaign)).order_by(Campaign.created_at.desc())
        ).mappings().all()]
    return {"campaigns": rows}


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int):
    with db.session() as s:
        row = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not row:
            raise HTTPException(404, "no such campaign")
        recipients_total = s.execute(
            select(func.count().label("n")).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id)
        ).mappings().first()["n"]
    return {"campaign": dict(row), "recipients_total": recipients_total}


@router.post("/campaigns")
def create_campaign(c_in: CampaignIn):
    if c_in.kind not in ("promotional", "transactional"):
        raise HTTPException(400, "kind must be 'promotional' or 'transactional'")
    sf = _scheduled_for_or_400(c_in.scheduled_for) if c_in.scheduled_for is not None else None
    with db.session() as s:
        tmpl = s.execute(select(Template.id).where(Template.id == c_in.template_id)).first()
        if not tmpl:
            raise HTTPException(404, "no such template")
        if c_in.entity_id is not None:
            if not s.execute(select(Entity.id).where(Entity.id == c_in.entity_id)).first():
                raise HTTPException(404, "no such company")
        initial_status = "scheduled" if c_in.scheduled_for else "draft"
        res = s.execute(
            insert(Campaign)
            .values(name=c_in.name, template_id=c_in.template_id, kind=c_in.kind, entity_id=c_in.entity_id,
                    scheduled_for=sf, status=initial_status,
                    rate_limit_per_min=c_in.rate_limit_per_min, bounce_pause_pct=c_in.bounce_pause_pct,
                    human_send=int(c_in.human_send), human_send_min_s=c_in.human_send_min_s,
                    human_send_max_s=c_in.human_send_max_s, human_send_count=c_in.human_send_count)
            .execution_options(synchronize_session=False)
        )
        s.commit()
        new_id = res.lastrowid
    db.audit(actor="operator", action="campaign.created", target=f"campaign:{new_id}")
    return {"id": new_id}


class AddRecipientsIn(BaseModel):
    contact_ids: list[int] | None = None
    by_tag: str | None = None      # match contacts whose tags contain this token


@router.post("/campaigns/{campaign_id}/recipients")
def add_recipients(campaign_id: int, body: AddRecipientsIn):
    with db.session() as s:
        if not s.execute(select(Campaign.id).where(Campaign.id == campaign_id)).first():
            raise HTTPException(404, "no such campaign")

        ids = set(body.contact_ids or [])
        if body.by_tag:
            tag = body.by_tag.strip()
            for row in s.execute(select(Contact.id, Contact.tags).where(Contact.tags.is_not(None))).mappings().all():
                if tag in (row["tags"] or "").split("|"):
                    ids.add(row["id"])

        added = 0
        for cid in ids:
            try:
                s.execute(
                    insert(CampaignRecipient).values(campaign_id=campaign_id, contact_id=cid)
                    .execution_options(synchronize_session=False)
                )
                added += 1
            except Exception as e:
                if "UNIQUE" in str(e):
                    continue
                raise
        s.execute(
            update(Campaign).where(Campaign.id == campaign_id)
            .values(recipient_count=select(func.count()).select_from(CampaignRecipient)
                    .where(CampaignRecipient.campaign_id == campaign_id).scalar_subquery(),
                    updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="campaign.recipients_added",
             target=f"campaign:{campaign_id}", details={"added": added})
    return {"added": added, "total_in_set": len(ids)}


class MarkTestedIn(BaseModel):
    """Operator confirms they sent + received a test, ready to launch."""
    pass


@router.post("/campaigns/{campaign_id}/mark-tested")
def mark_tested(campaign_id: int):
    """Set test_sent_at + capture template_hash_at_test (v1.7.6 gate). A scheduled campaign stays scheduled."""
    with db.session() as s:
        camp = s.execute(select(Campaign.template_id).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")
        tmpl = s.execute(
            select(Template.template_hash).where(Template.id == camp["template_id"])
        ).mappings().first()
        s.execute(
            update(Campaign).where(Campaign.id == campaign_id)
            .values(status=case((Campaign.status == "scheduled", "scheduled"), else_="tested"),
                    test_sent_at=func.datetime("now"),
                    template_hash_at_test=tmpl["template_hash"], updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="campaign.tested", target=f"campaign:{campaign_id}",
             details={"template_hash": tmpl["template_hash"]})
    return {"tested": True, "template_hash_at_test": tmpl["template_hash"]}


def parse_utc_naive(value: str) -> datetime:
    """A posted or stored time as naive UTC. ISO 8601 with "Z" or an offset is converted;
    a value without a zone is taken as UTC already. Raises ValueError otherwise."""
    try:
        when = datetime.fromisoformat((value or "").strip())
        if when.tzinfo is not None:
            when = when.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError) as e:
        raise ValueError(str(e)) from None
    return when


def utc_text(when: datetime) -> str:
    """The stored form "YYYY-MM-DD HH:MM:SS" (always a 4-digit year, seconds truncated)."""
    return when.isoformat(sep=" ", timespec="seconds")


_SCHEDULE_BAD_TIME = "Enter a valid date and time to launch."
_SCHEDULE_PAST = "Pick a time in the future. To send now, press Launch."
_SCHEDULE_NOT_TESTED = ("Only a tested campaign can be scheduled (this one is {status}). "
                        "Send a test and mark it tested first.")
_SCHEDULE_EDITED = ("The template was edited after the last test. Send a test and mark the campaign "
                    "tested again, then schedule it.")
_SCHEDULE_UNFINISHED = "This campaign has an unfinished send. Finish it with Re-launch before scheduling it."
_SCHEDULE_RACE = "The campaign changed while it was being scheduled. Reload the page and try again."


class ScheduleIn(BaseModel):
    scheduled_for: str   # ISO 8601; the UI posts toISOString() ("...Z"); no zone = UTC


@router.post("/campaigns/{campaign_id}/schedule")
def schedule_campaign(campaign_id: int, body: ScheduleIn):
    """Put a tested campaign on the schedule. test_sent_at and template_hash_at_test are kept,
    so the scheduler's launch passes the test gate; a template edited later is still refused there."""
    try:
        when = parse_utc_naive(body.scheduled_for)
    except ValueError:
        raise HTTPException(400, _SCHEDULE_BAD_TIME)
    if when <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(400, _SCHEDULE_PAST)
    sf = utc_text(when)
    with db.session() as s:
        camp = s.execute(select(Campaign.status, Campaign.test_sent_at, Campaign.template_hash_at_test,
                                Campaign.template_id, Campaign.relaunch_open)
                         .where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")
        if camp["status"] != "tested" or not camp["test_sent_at"]:
            raise HTTPException(400, _SCHEDULE_NOT_TESTED.format(status=camp["status"]))
        cur_hash = s.execute(select(Template.template_hash)
                             .where(Template.id == camp["template_id"])).scalar_one_or_none()
        if cur_hash != camp["template_hash_at_test"]:
            raise HTTPException(400, _SCHEDULE_EDITED)
        unfinished = camp["relaunch_open"] or s.execute(
            select(CampaignBatch.id).where(CampaignBatch.campaign_id == campaign_id,
                                           CampaignBatch.dispatch_started_at.is_not(None)).limit(1)).first()
        if unfinished:
            raise HTTPException(400, _SCHEDULE_UNFINISHED)
        moved = s.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.status == "tested",
                   Campaign.template_hash_at_test == camp["template_hash_at_test"])
            .values(status="scheduled", scheduled_for=sf, updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)).rowcount
        s.commit()
    if moved != 1:
        raise HTTPException(409, _SCHEDULE_RACE)
    db.audit(actor="operator", action="campaign.scheduled", target=f"campaign:{campaign_id}",
             details={"scheduled_for": sf})
    return {"scheduled": True, "scheduled_for": sf}


# Suppression (manual + read-only list)

class SuppressionAddIn(BaseModel):
    email: str
    reason: str = "manual"


@router.post("/suppression")
def add_suppression(body: SuppressionAddIn):
    if body.reason not in ("unsubscribe", "complaint", "bounce_hard", "manual", "erasure_request"):
        raise HTTPException(400, "bad reason")
    h = _email_hash(body.email)
    with db.session() as s:
        s.execute(
            insert(SuppressionHash).prefix_with("OR IGNORE")
            .values(email_hash=h, reason=body.reason, source="manual:admin")
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="suppression.added", target=f"hash:{h}",
             details={"reason": body.reason})
    return {"email_hash": h, "reason": body.reason}


@router.get("/suppression")
def list_suppression(limit: int = 500):
    with db.session() as s:
        rows = [dict(r) for r in s.execute(
            select(SuppressionHash.email_hash, SuppressionHash.reason, SuppressionHash.added_at, SuppressionHash.source)
            .order_by(SuppressionHash.added_at.desc()).limit(limit)
        ).mappings().all()]
        total = s.execute(select(func.count().label("n")).select_from(SuppressionHash)).mappings().first()["n"]
    return {"suppressions": rows, "total": total}


# Companies

class EntityIn(BaseModel):
    # Required for app function
    name: str                        # display name + {{companyName}} variable
    domain: str                      # sending domain (must be verified in Resend)
    from_name: str = ""              # "From" display name - required to send
    from_email: str = ""             # "From" address - required to send
    # Required by CAN-SPAM / Gmail EULA for commercial email
    footer_address: str | None = None  # physical mailing address in every footer
    # Optional
    support_email: str | None = None   # shown in compliance UI / contact block
    public_host: str | None = None     # base URL for unsubscribe/erasure links

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("company name must not be blank")
        return v


class EntityKeyIn(BaseModel):
    resend_api_key: str


def _entity_row(row: dict) -> dict:
    """Return safe dict - strips encrypted key, adds key_set flag."""
    return {
        "id":         row["id"],
        "name":       row["name"],
        "domain":     row["domain"],
        "from_name":  row["from_name"],
        "from_email": row["from_email"],
        "key_set":    bool(row.get("resend_key_enc")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/entities")
def list_entities():
    with db.session() as s:
        rows = [_entity_row(dict(r)) for r in s.execute(
            select(*cols(Entity)).order_by(Entity.name)
        ).mappings().all()]
    return {"entities": rows}


@router.get("/entities/{entity_id}")
def get_entity(entity_id: int):
    with db.session() as s:
        row = s.execute(select(*cols(Entity)).where(Entity.id == entity_id)).mappings().first()
    if not row:
        raise HTTPException(404, "no such company")
    return _entity_row(dict(row))


@router.post("/entities")
def create_entity(body: EntityIn):
    try:
        with db.session() as s:
            res = s.execute(
                insert(Entity)
                .values(name=body.name, domain=body.domain, from_name=body.from_name, from_email=body.from_email,
                        footer_address=body.footer_address, support_email=body.support_email,
                        public_host=body.public_host)
                .execution_options(synchronize_session=False)
            )
            s.commit()
            new_id = res.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "entity name already exists")
        raise
    db.audit(actor="operator", action="entity.created", target=f"entity:{new_id}")
    return {"id": new_id}


@router.patch("/entities/{entity_id}")
def update_entity(entity_id: int, body: EntityIn):
    with db.session() as s:
        if not s.execute(select(Entity.id).where(Entity.id == entity_id)).first():
            raise HTTPException(404, "no such entity")
        s.execute(
            update(Entity).where(Entity.id == entity_id)
            .values(name=body.name, domain=body.domain, from_name=body.from_name, from_email=body.from_email,
                    footer_address=body.footer_address, support_email=body.support_email,
                    public_host=body.public_host, updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="entity.updated", target=f"entity:{entity_id}")
    return {"updated": True}


@router.post("/entities/{entity_id}/set-key")
def set_entity_key(entity_id: int, body: EntityKeyIn):
    with db.session() as s:
        if not s.execute(select(Entity.id).where(Entity.id == entity_id)).first():
            raise HTTPException(404, "no such company")
        enc = _settings._encrypt(body.resend_api_key)
        s.execute(
            update(Entity).where(Entity.id == entity_id)
            .values(resend_key_enc=enc, updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="entity.key_set", target=f"company:{entity_id}")
    return {"key_set": True}


@router.delete("/entities/{entity_id}/key")
def clear_entity_key(entity_id: int):
    with db.session() as s:
        if not s.execute(select(Entity.id).where(Entity.id == entity_id)).first():
            raise HTTPException(404, "no such company")
        s.execute(
            update(Entity).where(Entity.id == entity_id)
            .values(resend_key_enc=None, updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="company.key_cleared", target=f"company:{entity_id}")
    return {"key_cleared": True}


@router.delete("/entities/{entity_id}")
def delete_company(entity_id: int):
    with db.session() as s:
        if not s.execute(select(Entity.id).where(Entity.id == entity_id)).first():
            raise HTTPException(404, "no such company")
        # disassociate campaigns rather than blocking delete
        s.execute(
            update(Campaign).where(Campaign.entity_id == entity_id).values(entity_id=None)
            .execution_options(synchronize_session=False)
        )
        s.execute(delete(Entity).where(Entity.id == entity_id).execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="entity.deleted", target=f"company:{entity_id}")
    return {"deleted": True}


# Campaign Analytics

@router.get("/campaigns/{campaign_id}/analytics/data")
def campaign_analytics_data(campaign_id: int):
    with db.session() as s:
        camp = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")

        total = s.execute(
            select(func.count().label("n")).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id)
        ).mappings().first()["n"]

        def _count(status_col: str) -> int:
            return s.execute(
                select(func.count().label("n")).select_from(CampaignRecipient)
                .where(CampaignRecipient.campaign_id == campaign_id,
                       getattr(CampaignRecipient, status_col).is_not(None))
            ).mappings().first()["n"]

        sent      = s.execute(
            select(func.count().label("n")).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id,
                   CampaignRecipient.status.in_(("sent", "delivered", "opened", "clicked")))
        ).mappings().first()["n"]
        failed    = s.execute(
            select(func.count().label("n")).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.status == "failed")
        ).mappings().first()["n"]
        queued    = s.execute(
            select(func.count().label("n")).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.status == "queued")
        ).mappings().first()["n"]
        opened    = _count("opened_at")
        clicked   = _count("clicked_at")
        bounced   = _count("bounced_at")
        complained = _count("complained_at")

        cr, con = aliased(CampaignRecipient), aliased(Contact)
        rows = [dict(r) for r in s.execute(
            select(cr.id, cr.contact_id, cr.status, cr.message_id, cr.sent_at, cr.opened_at,
                   cr.clicked_at, cr.bounced_at, cr.complained_at, cr.failure_reason,
                   con.email, con.name)
            .outerjoin(con, con.id == cr.contact_id)
            .where(cr.campaign_id == campaign_id).order_by(cr.id)
        ).mappings().all()]

    def _pct(n: int) -> str:
        return f"{round(n/sent*100)}%" if sent else "-"

    return {
        "campaign": dict(camp),
        "summary": {
            "total": total, "sent": sent, "failed": failed, "queued": queued,
            "opened": opened,    "opened_pct":    _pct(opened),
            "clicked": clicked,  "clicked_pct":   _pct(clicked),
            "bounced": bounced,  "bounced_pct":   _pct(bounced),
            "complained": complained, "complained_pct": _pct(complained),
        },
        "recipients": rows,
    }


@router.post("/campaigns/{campaign_id}/clone")
def clone_campaign(campaign_id: int):
    """Duplicate a campaign as a new draft (recipients not copied)."""
    with db.session() as s:
        src = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not src:
            raise HTTPException(404, "no such campaign")
        src = dict(src)
        res = s.execute(
            insert(Campaign)
            .values(name=f"{src['name']} (copy)", template_id=src["template_id"], kind=src["kind"],
                    entity_id=src.get("entity_id"), rate_limit_per_min=src.get("rate_limit_per_min"),
                    bounce_pause_pct=src.get("bounce_pause_pct") or 0.10,
                    human_send=src.get("human_send", 0), human_send_min_s=src.get("human_send_min_s", 60),
                    human_send_max_s=src.get("human_send_max_s", 210), human_send_count=src.get("human_send_count", 1),
                    status="draft")
            .execution_options(synchronize_session=False)
        )
        s.commit()
        new_id = res.lastrowid
    db.audit(actor="operator", action="campaign.cloned", target=f"campaign:{new_id}",
             details={"source": campaign_id})
    return {"id": new_id}


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: int):
    """Pause a scheduled or dispatched campaign. In-flight SQS packs are unaffected."""
    with db.session() as s:
        camp = s.execute(select(Campaign.status).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")
        if camp["status"] not in ("scheduled", "dispatched", "tested"):
            raise HTTPException(400, f"cannot pause campaign with status {camp['status']!r}")
        s.execute(update(Campaign).where(Campaign.id == campaign_id)
                  .values(status="paused", updated_at=func.datetime("now"))
                  .execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="campaign.paused", target=f"campaign:{campaign_id}")
    return {"paused": True}


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: int):
    """Resume a paused campaign back to its prior actionable state."""
    with db.session() as s:
        camp = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")
        if camp["status"] != "paused":
            raise HTTPException(400, "campaign is not paused")
        resume_status = "tested" if camp["test_sent_at"] else "draft"
        s.execute(update(Campaign).where(Campaign.id == campaign_id)
                  .values(status=resume_status, updated_at=func.datetime("now"))
                  .execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="campaign.resumed", target=f"campaign:{campaign_id}")
    return {"resumed": True, "status": resume_status}


@router.post("/campaigns/{campaign_id}/retry")
def retry_failed(campaign_id: int):
    """Reset failed recipients to 'queued' and set campaign back to 'tested' so it can relaunch."""
    with db.session() as s:
        if not s.execute(select(Campaign.id).where(Campaign.id == campaign_id)).first():
            raise HTTPException(404, "no such campaign")
        result = s.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.status == "failed")
            .values(status="queued", failure_reason=None)
            .execution_options(synchronize_session=False)
        )
        reset_count = result.rowcount
        if reset_count:
            s.execute(
                update(Campaign).where(Campaign.id == campaign_id)
                .values(status="tested", updated_at=func.datetime("now"))
                .execution_options(synchronize_session=False)
            )
        s.commit()
    db.audit(actor="operator", action="campaign.retry_failed",
             target=f"campaign:{campaign_id}", details={"reset": reset_count})
    return {"reset": reset_count}


# Operators (Feature 10)

class OperatorIn(BaseModel):
    email: str
    password: str
    role: str = "operator"


@router.get("/operators", dependencies=[Depends(_require_admin)])
def list_operators():
    with db.session() as s:
        rows = [dict(r) for r in s.execute(
            select(Operator.id, Operator.email, Operator.role, Operator.active, Operator.created_at,
                   Operator.last_login_at).order_by(Operator.id)
        ).mappings().all()]
    return {"operators": rows}


@router.post("/operators", dependencies=[Depends(_require_admin)])
def create_operator(body: OperatorIn, session=Depends(require_session)):
    import bcrypt as _bcrypt
    if body.role not in ("admin", "operator"):
        raise HTTPException(400, "role must be admin or operator")
    h = _bcrypt.hashpw(body.password.encode(), _bcrypt.gensalt(rounds=12)).decode()
    with db.session() as s:
        try:
            res = s.execute(
                insert(Operator).values(email=body.email.strip().lower(), password_hash=h, role=body.role)
                .execution_options(synchronize_session=False)
            )
            s.commit()
            new_id = res.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "email already exists")
            raise
    db.audit(actor="operator", action="operator.created", target=f"operator:{new_id}")
    return {"id": new_id}


@router.post("/operators/{op_id}/toggle-active", dependencies=[Depends(_require_admin)])
def toggle_operator_active(op_id: int):
    with db.session() as s:
        row = s.execute(select(Operator.active).where(Operator.id == op_id)).mappings().first()
        if not row:
            raise HTTPException(404, "no such operator")
        new_state = 0 if row["active"] else 1
        s.execute(update(Operator).where(Operator.id == op_id).values(active=new_state)
                  .execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="operator.toggled", target=f"operator:{op_id}",
             details={"active": new_state})
    return {"active": new_state}


@router.delete("/operators/{op_id}", dependencies=[Depends(_require_admin)])
def delete_operator(op_id: int, session=Depends(require_session)):
    with db.session() as s:
        if not s.execute(select(Operator.id).where(Operator.id == op_id)).first():
            raise HTTPException(404, "no such operator")
        s.execute(delete(Operator).where(Operator.id == op_id).execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="operator.deleted", target=f"operator:{op_id}")
    return {"deleted": True}


def get_entity_credentials(entity_id: int) -> dict:
    """Used by launch.py - returns {resend_key, from_email, from_name, domain} or raises."""
    with db.session() as s:
        row = s.execute(select(*cols(Entity)).where(Entity.id == entity_id)).mappings().first()
    if not row:
        raise ValueError(f"company {entity_id} not found")
    if not row["resend_key_enc"]:
        raise ValueError(f"company '{row['name']}' has no Resend API key configured")
    return {
        "resend_key": _settings._decrypt(row["resend_key_enc"]),
        "from_name":  row["from_name"] or row["name"],
        "from_email": row["from_email"] or f"hello@{row['domain']}",
        "domain":     row["domain"],
    }


# Entity test connection

@router.post("/entities/{entity_id}/test-connection")
def test_entity_connection(entity_id: int):
    """Fire a real Resend API call to verify key + domain. Returns pass/fail + detail."""
    import httpx as _httpx
    with db.session() as s:
        row = s.execute(select(*cols(Entity)).where(Entity.id == entity_id)).mappings().first()
    if not row:
        raise HTTPException(404, "no such entity")
    if not row["resend_key_enc"]:
        return {"ok": False, "detail": "No Resend API key configured for this entity."}
    try:
        key = _settings._decrypt(row["resend_key_enc"])
        r = _httpx.get("https://api.resend.com/domains",
                       headers={"Authorization": f"Bearer {key}"}, timeout=8)
        if r.status_code == 401:
            return {"ok": False, "detail": "API key rejected by Resend (401 Unauthorized)."}
        if r.status_code >= 400:
            return {"ok": False, "detail": f"Resend returned HTTP {r.status_code}."}
        domains = [d.get("name") for d in r.json().get("data", [])]
        domain_ok = row["domain"] in domains
        return {
            "ok": True,
            "domain_verified": domain_ok,
            "domains": domains,
            "detail": f"Key valid. Domain '{row['domain']}' {'\u2713 verified' if domain_ok else '\u2717 not found in Resend account'} among {len(domains)} domain(s).",
        }
    except Exception as e:
        return {"ok": False, "detail": f"Connection error: {e}"}


# Quick send

class QuickSendIn(BaseModel):
    to_email: str
    subject: str
    html_body: str
    entity_id: int | None = None
    from_name: str = ""
    from_email: str = ""


@router.post("/quicksend")
def quicksend(body: QuickSendIn):
    """Fire a single email directly via Resend - no campaign, no contact tracking."""
    import httpx as _httpx
    if "@" not in body.to_email:
        raise HTTPException(400, "invalid to_email")
    if "{{" in body.subject or "{{" in body.html_body:
        raise HTTPException(400, "Quick send does not fill variables - remove every {{...}} from the subject and body")

    resend_key = _settings.get("resend_api_key", "")
    from_addr = _settings.get("email_from", "") or "noreply@example.com"
    from_name_val = body.from_name
    from_email_val = body.from_email

    if body.entity_id:
        with db.session() as s:
            row = s.execute(select(*cols(Entity)).where(Entity.id == body.entity_id)).mappings().first()
        if row and row["resend_key_enc"]:
            resend_key = _settings._decrypt(row["resend_key_enc"])
        if row:
            from_name_val = from_name_val or row["from_name"] or row["name"]
            from_email_val = from_email_val or row["from_email"] or f"hello@{row['domain']}"
            from_addr = f"{from_name_val} <{from_email_val}>" if from_name_val else from_email_val

    if not resend_key:
        raise HTTPException(400, "No Resend API key configured. Set one in Settings or on an Entity.")

    try:
        r = _httpx.post("https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {resend_key}"},
                        json={"from": from_addr, "to": [body.to_email],
                              "subject": body.subject, "html": body.html_body},
                        timeout=15)
        if r.status_code >= 400:
            raise HTTPException(400, f"Resend rejected: {r.text[:200]}")
        return {"sent": True, "message_id": r.json().get("id"), "to": body.to_email}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"send failed: {e}")


# Schedule view data

@router.get("/schedule")
def get_schedule():
    """All scheduled + active + paused + recent campaigns for the schedule tab."""
    with db.session() as s:
        c, t, e = aliased(Campaign), aliased(Template), aliased(Entity)
        scheduled = [dict(r) for r in s.execute(
            select(*cols(c), t.name.label("template_name"), e.name.label("entity_name"))
            .outerjoin(t, t.id == c.template_id)
            .outerjoin(e, e.id == c.entity_id)
            .where(c.status == "scheduled").order_by(func.datetime(c.scheduled_for).asc())
        ).mappings().all()]
        active = [dict(r) for r in s.execute(
            select(*cols(c), t.name.label("template_name"), e.name.label("entity_name"),
                   select(func.count()).select_from(CampaignRecipient)
                   .where(CampaignRecipient.campaign_id == c.id, CampaignRecipient.status == "queued")
                   .scalar_subquery().label("queued_count"),
                   select(func.count()).select_from(CampaignRecipient)
                   .where(CampaignRecipient.campaign_id == c.id,
                          CampaignRecipient.status.in_(("sent", "opened", "clicked")))
                   .scalar_subquery().label("sent_count"))
            .outerjoin(t, t.id == c.template_id)
            .outerjoin(e, e.id == c.entity_id)
            .where(c.status == "dispatched").order_by(c.dispatched_at.desc()).limit(10)
        ).mappings().all()]
        paused = [dict(r) for r in s.execute(
            select(*cols(c), t.name.label("template_name"), e.name.label("entity_name"))
            .outerjoin(t, t.id == c.template_id)
            .outerjoin(e, e.id == c.entity_id)
            .where(c.status == "paused").order_by(c.updated_at.desc())
        ).mappings().all()]
        recent = [dict(r) for r in s.execute(
            select(*cols(c), t.name.label("template_name"), e.name.label("entity_name"))
            .outerjoin(t, t.id == c.template_id)
            .outerjoin(e, e.id == c.entity_id)
            .where(c.status == "dispatched", c.dispatched_at >= func.datetime("now", "-7 days"))
            .order_by(c.dispatched_at.desc()).limit(20)
        ).mappings().all()]
    return {"scheduled": scheduled, "active": active, "paused": paused, "recent": recent}


# Campaign stages (multi-stage / drip)

class StageIn(BaseModel):
    template_id: int
    scheduled_for: str   # ISO datetime
    note: str | None = None


@router.get("/campaigns/{campaign_id}/stages")
def list_stages(campaign_id: int):
    with db.session() as s:
        if not s.execute(select(Campaign.id).where(Campaign.id == campaign_id)).first():
            raise HTTPException(404, "no such campaign")
        cs, t = aliased(CampaignStage), aliased(Template)
        rows = [dict(r) for r in s.execute(
            select(*cols(cs), t.name.label("template_name"))
            .join(t, t.id == cs.template_id)
            .where(cs.campaign_id == campaign_id).order_by(cs.stage_number)
        ).mappings().all()]
    return {"stages": rows}


@router.post("/campaigns/{campaign_id}/stages")
def add_stage(campaign_id: int, body: StageIn):
    sf = _scheduled_for_or_400(body.scheduled_for)
    with db.session() as s:
        if not s.execute(select(Campaign.id).where(Campaign.id == campaign_id)).first():
            raise HTTPException(404, "no such campaign")
        if not s.execute(select(Template.id).where(Template.id == body.template_id)).first():
            raise HTTPException(404, "no such template")
        next_num = (s.execute(select(func.coalesce(func.max(CampaignStage.stage_number), 0) + 1)
                              .where(CampaignStage.campaign_id == campaign_id)).scalar_one())
        res = s.execute(
            insert(CampaignStage)
            .values(campaign_id=campaign_id, stage_number=next_num, template_id=body.template_id,
                    scheduled_for=sf, note=body.note)
            .execution_options(synchronize_session=False)
        )
        s.commit()
        new_id = res.lastrowid
    db.audit(actor="operator", action="campaign.stage_added",
             target=f"campaign:{campaign_id}", details={"stage_id": new_id})
    return {"id": new_id, "stage_number": next_num}


@router.patch("/campaigns/{campaign_id}/stages/{stage_id}")
def update_stage(campaign_id: int, stage_id: int, body: StageIn):
    sf = _scheduled_for_or_400(body.scheduled_for)
    with db.session() as s:
        if not s.execute(select(CampaignStage.id).where(CampaignStage.id == stage_id,
                                                        CampaignStage.campaign_id == campaign_id)).first():
            raise HTTPException(404, "no such stage")
        s.execute(
            update(CampaignStage).where(CampaignStage.id == stage_id)
            .values(template_id=body.template_id, scheduled_for=sf, note=body.note)
            .execution_options(synchronize_session=False)
        )
        s.commit()
    return {"updated": True}


@router.delete("/campaigns/{campaign_id}/stages/{stage_id}")
def delete_stage(campaign_id: int, stage_id: int):
    with db.session() as s:
        if not s.execute(select(CampaignStage.id).where(CampaignStage.id == stage_id,
                                                        CampaignStage.campaign_id == campaign_id)).first():
            raise HTTPException(404, "no such stage")
        s.execute(delete(CampaignStage).where(CampaignStage.id == stage_id)
                  .execution_options(synchronize_session=False))
        # Renumber remaining stages
        remaining = s.execute(
            select(CampaignStage.id).where(CampaignStage.campaign_id == campaign_id)
            .order_by(CampaignStage.stage_number)
        ).mappings().all()
        for i, r in enumerate(remaining, 1):
            s.execute(update(CampaignStage).where(CampaignStage.id == r["id"]).values(stage_number=i)
                      .execution_options(synchronize_session=False))
        s.commit()
    return {"deleted": True}


# Selective template export / bulk import

@router.get("/templates/export")
def export_templates(ids: str | None = None):
    from fastapi.responses import Response
    with db.session() as s:
        if ids:
            id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
            rows = s.execute(
                select(Template.name, Template.subject, Template.from_name, Template.html_body, Template.text_body,
                       Template.tracking_enabled)
                .where(Template.id.in_(id_list)).order_by(Template.id)
            ).mappings().all() if id_list else []
        else:
            rows = s.execute(
                select(Template.name, Template.subject, Template.from_name, Template.html_body, Template.text_body,
                       Template.tracking_enabled)
                .order_by(Template.id)
            ).mappings().all()
    import json as _json
    items = [dict(r) for r in rows]
    body = _json.dumps(items, indent=2, ensure_ascii=False)
    fname = "templates-selected.json" if ids else "templates.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@router.post("/templates/bulk-import")
async def bulk_import_templates(file: UploadFile = File(...)):
    """Import a JSON array of template objects."""
    import json as _json
    raw = await file.read()
    try:
        items = _json.loads(raw)
        if not isinstance(items, list):
            items = [items]
    except Exception as e:
        raise HTTPException(400, f"JSON parse error: {e}")
    imported, skipped, errors = 0, 0, []
    for i, item in enumerate(items):
        try:
            t = TemplateIn(
                name=item["name"], subject=item["subject"],
                from_name=item.get("from_name", ""), html_body=item.get("html_body", ""),
                text_body=item.get("text_body"), tracking_enabled=item.get("tracking_enabled", True),
            )
            create_template(t)
            imported += 1
        except HTTPException as e:
            if e.status_code == 409:
                skipped += 1
            else:
                errors.append(f"item {i}: {e.detail}")
        except Exception as e:
            errors.append(f"item {i}: {e}")
    return {"imported": imported, "skipped_duplicate": skipped, "errors": errors}


# Selective entity export / import

@router.get("/entities/export")
def export_entities(ids: str | None = None):
    from fastapi.responses import Response
    import json as _json
    with db.session() as s:
        if ids:
            id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
            rows = s.execute(
                select(Entity.name, Entity.domain, Entity.from_name, Entity.from_email, Entity.footer_address,
                       Entity.support_email, Entity.public_host)
                .where(Entity.id.in_(id_list)).order_by(Entity.id)
            ).mappings().all() if id_list else []
        else:
            rows = s.execute(
                select(Entity.name, Entity.domain, Entity.from_name, Entity.from_email, Entity.footer_address,
                       Entity.support_email, Entity.public_host)
                .order_by(Entity.id)
            ).mappings().all()
    items = [dict(r) for r in rows]
    # Note: resend_key_enc is deliberately excluded - keys are not portable
    body = _json.dumps(items, indent=2, ensure_ascii=False)
    fname = "entities-selected.json" if ids else "entities.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@router.post("/entities/bulk-import")
async def bulk_import_entities(file: UploadFile = File(...)):
    """Import a JSON array of entity objects (Resend keys not included - set manually after import)."""
    import json as _json
    raw = await file.read()
    try:
        items = _json.loads(raw)
        if not isinstance(items, list):
            items = [items]
    except Exception as e:
        raise HTTPException(400, f"JSON parse error: {e}")
    imported, skipped, errors = 0, 0, []
    for i, item in enumerate(items):
        try:
            e = EntityIn(
                name=item["name"], domain=item["domain"],
                from_name=item.get("from_name", ""), from_email=item.get("from_email", ""),
                footer_address=item.get("footer_address"), support_email=item.get("support_email"),
                public_host=item.get("public_host"),
            )
            create_entity(e)
            imported += 1
        except HTTPException as ex:
            if ex.status_code == 409:
                skipped += 1
            else:
                errors.append(f"item {i}: {ex.detail}")
        except Exception as ex:
            errors.append(f"item {i}: {ex}")
    return {"imported": imported, "skipped_duplicate": skipped, "errors": errors}


# Campaign edit

class CampaignEditIn(BaseModel):
    name: str | None = None
    entity_id: int | None = None
    rate_limit_per_min: int | None = None
    bounce_pause_pct: float | None = None
    human_send: bool | None = None
    human_send_min_s: int | None = None
    human_send_max_s: int | None = None
    human_send_count: int | None = None
    scheduled_for: str | None = None


@router.patch("/campaigns/{campaign_id}")
def edit_campaign(campaign_id: int, body: CampaignEditIn):
    sf = _scheduled_for_or_400(body.scheduled_for) if body.scheduled_for is not None else None
    with db.session() as s:
        camp = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")
        camp = dict(camp)
        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.entity_id is not None:
            if not s.execute(select(Entity.id).where(Entity.id == body.entity_id)).first():
                raise HTTPException(404, "no such entity")
            updates["entity_id"] = body.entity_id
        if body.rate_limit_per_min is not None:
            updates["rate_limit_per_min"] = body.rate_limit_per_min
        if body.bounce_pause_pct is not None:
            updates["bounce_pause_pct"] = body.bounce_pause_pct
        if body.human_send is not None:
            updates["human_send"] = int(body.human_send)
        if body.human_send_min_s is not None:
            updates["human_send_min_s"] = body.human_send_min_s
        if body.human_send_max_s is not None:
            updates["human_send_max_s"] = body.human_send_max_s
        if body.human_send_count is not None:
            updates["human_send_count"] = body.human_send_count
        if body.scheduled_for is not None:
            updates["scheduled_for"] = sf
        if not updates:
            return {"updated": False, "reason": "no fields provided"}
        s.execute(
            update(Campaign).where(Campaign.id == campaign_id)
            .values(**updates, updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="campaign.edited", target=f"campaign:{campaign_id}",
             details={"fields": list(updates.keys())})
    return {"updated": True}


# Contact edit

class ContactEditIn(BaseModel):
    name: str | None = None
    tags: str | None = None


@router.patch("/contacts/{contact_id}")
def edit_contact(contact_id: int, body: ContactEditIn):
    with db.session() as s:
        row = s.execute(select(Contact.id).where(Contact.id == contact_id)).first()
        if not row:
            raise HTTPException(404, "no such contact")
        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.tags is not None:
            updates["tags"] = body.tags
        if not updates:
            return {"updated": False, "reason": "no fields provided"}
        s.execute(update(Contact).where(Contact.id == contact_id).values(**updates)
                  .execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="contact.edited", target=f"contact:{contact_id}",
             details={"fields": list(updates.keys())})
    return {"updated": True}


# Template delete

@router.delete("/templates/{template_id}")
def delete_template(template_id: int):
    with db.session() as s:
        if not s.execute(select(Template.id).where(Template.id == template_id)).first():
            raise HTTPException(404, "no such template")
        in_use = s.execute(
            select(Campaign.id).where(Campaign.template_id == template_id).limit(1)
        ).first()
        if in_use:
            raise HTTPException(409, "template is referenced by one or more campaigns")
        s.execute(delete(Template).where(Template.id == template_id).execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="template.deleted", target=f"template:{template_id}")
    return {"deleted": True}


# Campaign archive

@router.post("/campaigns/{campaign_id}/archive")
async def archive_campaign(campaign_id: int):
    """A campaign whose mail is still with the cloud (a batch pending, scheduled or sending, an
    interrupted batch, or an unfinished Re-launch) is first cancelled exactly as Cancel does."""
    with db.session() as s:
        relaunch_open = s.execute(select(Campaign.relaunch_open).where(Campaign.id == campaign_id)).scalar_one_or_none()
        if relaunch_open is None:
            raise HTTPException(404, "no such campaign")
        live = bool(relaunch_open) or s.execute(
            select(CampaignBatch.id).where(
                CampaignBatch.campaign_id == campaign_id,
                CampaignBatch.status.in_(("pending", "scheduled", "sending")) | CampaignBatch.dispatch_started_at.is_not(None))
            .limit(1)).first() is not None
    if live:
        await cancel_campaign(campaign_id)
    with db.session() as s:
        s.execute(
            update(Campaign).where(Campaign.id == campaign_id)
            .values(status="archived", updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
    db.audit(actor="operator", action="campaign.archived", target=f"campaign:{campaign_id}")
    return {"archived": True}


# Add contacts to campaign

class AddToCampaignIn(BaseModel):
    campaign_id: int
    contact_ids: list[int]


@router.post("/contacts/add-to-campaign")
def add_contacts_to_campaign(body: AddToCampaignIn):
    with db.session() as s:
        if not s.execute(select(Campaign.id).where(Campaign.id == body.campaign_id)).first():
            raise HTTPException(404, "no such campaign")
        added = 0
        for cid in body.contact_ids:
            res = s.execute(
                insert(CampaignRecipient).prefix_with("OR IGNORE")
                .values(campaign_id=body.campaign_id, contact_id=cid)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount:
                added += 1
        s.execute(
            update(Campaign).where(Campaign.id == body.campaign_id)
            .values(recipient_count=select(func.count()).select_from(CampaignRecipient)
                    .where(CampaignRecipient.campaign_id == body.campaign_id).scalar_subquery(),
                    updated_at=func.datetime("now"))
            .execution_options(synchronize_session=False)
        )
        s.commit()
        total = s.execute(
            select(func.count().label("n")).select_from(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == body.campaign_id)
        ).mappings().first()["n"]
    db.audit(actor="operator", action="campaign.recipients_added",
             target=f"campaign:{body.campaign_id}", details={"added": added})
    return {"added": added, "total": total}


# Tags

@router.get("/tags")
def list_tags():
    return {"tags": list_tags_data()}


def list_tags_data() -> list:
    counts: dict[str, int] = {}
    with db.session() as s:
        for row in s.execute(select(Contact.tags).where(Contact.tags.is_not(None))).mappings().all():
            for tag in (row["tags"] or "").split("|"):
                tag = tag.strip()
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
    return [{"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda x: x[1], reverse=True)]


class TagRenameIn(BaseModel):
    old_tag: str
    new_tag: str


@router.post("/tags/rename")
def rename_tag(body: TagRenameIn):
    old = body.old_tag.strip()
    new = body.new_tag.strip()
    if not old or not new:
        raise HTTPException(400, "old_tag and new_tag must not be empty")
    updated = 0
    with db.session() as s:
        rows = s.execute(
            select(Contact.id, Contact.tags).where(Contact.tags.like(f"%{old}%"))
        ).mappings().all()
        for row in rows:
            tags = (row["tags"] or "").split("|")
            new_tags = [new if t.strip() == old else t for t in tags]
            if new_tags != tags:
                s.execute(update(Contact).where(Contact.id == row["id"]).values(tags="|".join(new_tags))
                          .execution_options(synchronize_session=False))
                updated += 1
        s.commit()
    db.audit(actor="operator", action="tags.renamed",
             details={"old_tag": old, "new_tag": new, "updated": updated})
    return {"updated": updated}


class TagDeleteIn(BaseModel):
    tag: str


@router.post("/tags/delete")
def delete_tag(body: TagDeleteIn):
    tag = body.tag.strip()
    if not tag:
        raise HTTPException(400, "tag must not be empty")
    updated = 0
    with db.session() as s:
        rows = s.execute(
            select(Contact.id, Contact.tags).where(Contact.tags.like(f"%{tag}%"))
        ).mappings().all()
        for row in rows:
            tags = [t for t in (row["tags"] or "").split("|") if t.strip() != tag]
            new_tags_str = "|".join(tags) if tags else None
            s.execute(update(Contact).where(Contact.id == row["id"]).values(tags=new_tags_str)
                      .execution_options(synchronize_session=False))
            updated += 1
        s.commit()
    db.audit(actor="operator", action="tags.deleted",
             details={"tag": tag, "updated": updated})
    return {"updated": updated}


# Campaign test-send-chain checkpoint

@router.get("/campaigns/{campaign_id}/test-send-chain")
def test_send_chain(campaign_id: int):
    from mailman.terminal import encryption
    result: dict = {
        "template_ok":  False, "template_error": None,
        "k_temp_ok":    False, "k_temp_error":   None,
        "cloud_ok":     False, "cloud_error":    None,
        "entity_ok":    False, "entity_error":   None,
        "unsub_ok":     False, "unsub_error":    None,
        "tracking_ok":  True,  "tracking_note":  "click_tracking on (body buttons); opens NOT tracked (bot noise)",
    }

    with db.session() as s:
        camp = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")
        camp = dict(camp)

        # (a) Template check
        tmpl = s.execute(
            select(Template.html_body).where(Template.id == camp["template_id"])
        ).mappings().first()
        if not tmpl:
            result["template_error"] = "template not found"
        elif not tmpl["html_body"]:
            result["template_error"] = "template has no html_body"
        else:
            result["template_ok"] = True

        # (d) Entity / Resend key check
        entity_id = camp.get("entity_id")
        if entity_id is None:
            result["entity_error"] = "no entity_id set on campaign"
        else:
            ent = s.execute(select(*cols(Entity)).where(Entity.id == entity_id)).mappings().first()
            if not ent:
                result["entity_error"] = f"entity {entity_id} not found"
            elif not ent["resend_key_enc"]:
                result["entity_error"] = f"entity '{ent['name']}' has no Resend API key"
            else:
                result["entity_ok"] = True

    # (b) K_temp file check
    k_temp_path = encryption.KEYS_DIR / "k_temp.bin"
    if not k_temp_path.exists():
        result["k_temp_error"] = f"k_temp.bin not found at {k_temp_path}"
    else:
        result["k_temp_ok"] = True

    # (c) Cloud bearer check
    cloud_bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    if not cloud_bearer_path.exists():
        result["cloud_error"] = f"cloud_bearer.txt not found at {cloud_bearer_path}"
    else:
        result["cloud_ok"] = True

    # (e) Unsub endpoint reachable
    from mailman.terminal import settings as _s
    public_host = _s.get("public_host") or ""
    if result["cloud_ok"] and public_host:
        result["unsub_ok"] = True
    elif not public_host:
        result["unsub_error"] = "public_host not set in settings"
    else:
        result["unsub_error"] = "cloud unreachable - unsub links won't resolve"

    return result


class TestSendIn(BaseModel):
    to_email: str


@router.post("/campaigns/{campaign_id}/test-send")
def campaign_test_send(campaign_id: int, body: TestSendIn):
    """Send the campaign's own mail to one address - the pack a launch would build for it, posted
    the way the dispatcher posts it, after the launch's refusals (launch.build_test_mail)."""
    import httpx as _httpx
    from mailman.terminal import launch as _launch

    to = body.to_email.strip()
    if "@" not in to:
        raise HTTPException(400, "invalid email")
    with db.session() as s:
        camp = s.execute(select(*cols(Campaign)).where(Campaign.id == campaign_id)).mappings().first()
        if not camp:
            raise HTTPException(404, "no such campaign")
        camp = dict(camp)
        tmpl = s.execute(select(*cols(Template)).where(Template.id == camp["template_id"])).mappings().first()
        if not tmpl:
            raise HTTPException(404, "template not found")
        tmpl = dict(tmpl)
    try:
        resend_key, payload = _launch.build_test_mail(camp, tmpl, to)
    except _launch.LaunchError as e:
        raise HTTPException(400, str(e))
    try:
        r = _httpx.post("https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {resend_key}"}, json=payload, timeout=15)
        if r.status_code >= 400:
            raise HTTPException(400, f"Resend rejected: {r.text[:300]}")
        message_id = r.json().get("id")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"send failed: {e}")
    db.audit(actor="operator", action="campaign.test_sent", target=f"campaign:{campaign_id}",
             details={"template_hash": tmpl["template_hash"], "message_id": message_id})
    return {"sent": True, "message_id": message_id, "to": to}


class RelaunchIn(BaseModel):
    expected_seq: int | None = None


@router.post("/campaigns/{campaign_id}/relaunch")
async def relaunch_campaign(campaign_id: int, body: RelaunchIn | None = None):
    """Re-launch: finish an interrupted send first, else resend to everyone.
    expected_seq (the relaunch_seq the page showed) makes a press from a stale page a 409."""
    from mailman.terminal import launch as _launch
    try:
        return await _launch.launch_campaign(campaign_id, actor="operator:relaunch", relaunch=True,
                                             expected_seq=body.expected_seq if body else None)
    except _launch.RelaunchConflict as e:
        raise HTTPException(409, str(e))
    except IntegrityError:
        raise HTTPException(409, _launch._concurrent_press_detail("relaunch"))
    except _launch.LaunchError as e:
        raise HTTPException(400, str(e))
    except SQLAlchemyError:
        log.exception("relaunch failed")
        raise HTTPException(500, "relaunch failed: a database error; the details are in the terminal log")
    except Exception as e:
        raise HTTPException(500, f"relaunch failed: {e}")


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: int):
    """Abort a scheduled/sending campaign: delete its still-pending packs from the
    cloud (DELETE /packs cancels only if still pending - already-sent are untouched)
    and mark the campaign + batches cancelled. Needed because batched sends sit
    deferred on the cloud until their send_at."""
    import os
    import httpx
    from mailman.terminal import encryption
    cloud = os.environ.get("CLOUD_URL", "")
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    bearer = bearer_path.read_text().strip() if bearer_path.exists() else ""
    # Cancel by campaign_id on the cloud - catches ALL pending packs including salted
    # seed packs (which have no dispatched_job row, so per-pack deletion would miss them).
    cancelled, cloud_ok = 0, False
    if cloud and bearer:
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                r = await client.post(f"{cloud}/packs/cancel-campaign",
                                      json={"campaign_id": campaign_id},
                                      headers={"Authorization": f"Bearer {bearer}"})
                if r.status_code == 200:
                    cancelled = r.json().get("cancelled", 0)
                    cloud_ok = True
            except Exception:
                pass
    from mailman.terminal import launch as _launch
    clear = cloud_ok and not _launch.push_in_flight(campaign_id)   # only a confirmed cancel, and no push racing it
    with db.session() as s:
        s.execute(update(Campaign).where(Campaign.id == campaign_id)
                  .values(status="cancelled", updated_at=func.datetime("now"), **({"relaunch_open": 0} if clear else {}))
                  .execution_options(synchronize_session=False))
        s.execute(update(CampaignBatch)
                  .where(CampaignBatch.campaign_id == campaign_id,
                         CampaignBatch.status.in_(("pending", "scheduled", "sending")))
                  .values(status="cancelled")
                  .execution_options(synchronize_session=False))
        if clear:
            # the cloud cancelled every pending pack, recorded or not; the next Re-launch is a new send
            s.execute(update(CampaignBatch).where(CampaignBatch.campaign_id == campaign_id)
                      .values(dispatch_started_at=None).execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="campaign.cancelled",
             target=f"campaign:{campaign_id}", details={"packs_cancelled": cancelled, "markers_cleared": clear})
    return {"ok": True, "packs_cancelled": cancelled}


# v3.22 batches + approve-next

@router.get("/campaigns/{campaign_id}/batches")
def list_batches(campaign_id: int):
    with db.session() as s:
        rows = [dict(r) for r in s.execute(
            select(*cols(CampaignBatch)).where(CampaignBatch.campaign_id == campaign_id)
            .order_by(CampaignBatch.batch_no)
        ).mappings().all()]
    # which batch is next-approvable?
    nxt = next((b for b in rows if b["status"] == "pending"), None)
    return {"batches": rows, "next_pending": nxt["batch_no"] if nxt else None}


@router.post("/campaigns/{campaign_id}/batches/{batch_no}/approve")
async def approve_batch(campaign_id: int, batch_no: int):
    """Manually release the next pending batch - gated by the analytics cooldown
    (approve_unlock_at, default = next day's window)."""
    from mailman.terminal import launch as _launch
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session() as s:
        b = s.execute(select(*cols(CampaignBatch)).where(CampaignBatch.campaign_id == campaign_id,
                                                        CampaignBatch.batch_no == batch_no)).mappings().first()
        if not b:
            raise HTTPException(404, "no such batch")
        b = dict(b)
        if b["status"] != "pending":
            raise HTTPException(400, f"batch {batch_no} is '{b['status']}', not pending")
        # the previous batch must be drained + cooldown elapsed
        prev = s.execute(select(*cols(CampaignBatch)).where(CampaignBatch.campaign_id == campaign_id,
                                                           CampaignBatch.batch_no == batch_no - 1)).mappings().first()
    if prev:
        prev = dict(prev)
        if prev["status"] not in ("drained", "cooldown", "done"):
            raise HTTPException(400, f"previous batch {batch_no-1} not drained yet "
                                     f"(status '{prev['status']}')")
        unlock = prev.get("approve_unlock_at")
        if unlock and now < unlock:
            raise HTTPException(400, f"cooldown active - approve-next unlocks at {unlock} UTC")
    try:
        result = await _launch.dispatch_batch(campaign_id, batch_no)
    except _launch.LaunchError as e:
        raise HTTPException(400, str(e))
    except IntegrityError:
        raise HTTPException(409, _launch._concurrent_press_detail("approve"))
    with db.session() as s:
        s.execute(update(CampaignBatch)
                  .where(CampaignBatch.campaign_id == campaign_id, CampaignBatch.batch_no == batch_no)
                  .values(approved_at=func.datetime("now"))
                  .execution_options(synchronize_session=False))
        if prev:
            s.execute(update(CampaignBatch)
                      .where(CampaignBatch.campaign_id == campaign_id, CampaignBatch.batch_no == batch_no - 1)
                      .values(status="done")
                      .execution_options(synchronize_session=False))
        s.commit()
    db.audit(actor="operator", action="batch.approved",
             target=f"campaign:{campaign_id}:batch:{batch_no}", details=result)
    return result


# Seed addresses (inbox-placement monitors)

class SeedIn(BaseModel):
    email: str
    provider: str | None = None


@router.get("/seeds")
def list_seeds():
    with db.session() as s:
        return {"seeds": [dict(r) for r in s.execute(
            select(*cols(SeedAddress)).order_by(SeedAddress.id)).mappings().all()]}


@router.post("/seeds")
def add_seed(body: SeedIn):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "invalid email")
    with db.session() as s:
        s.execute(insert(SeedAddress).prefix_with("OR IGNORE").values(email=email, provider=body.provider)
                  .execution_options(synchronize_session=False))
        s.commit()
        row = s.execute(select(*cols(SeedAddress)).where(SeedAddress.email == email)).mappings().first()
    return dict(row)


@router.post("/seeds/{seed_id}/toggle")
def toggle_seed(seed_id: int):
    with db.session() as s:
        s.execute(update(SeedAddress).where(SeedAddress.id == seed_id).values(active=1 - SeedAddress.active)
                  .execution_options(synchronize_session=False))
        s.commit()
        row = s.execute(select(*cols(SeedAddress)).where(SeedAddress.id == seed_id)).mappings().first()
    if not row:
        raise HTTPException(404, "no such seed")
    return dict(row)


@router.delete("/seeds/{seed_id}")
def delete_seed(seed_id: int):
    with db.session() as s:
        s.execute(delete(SeedAddress).where(SeedAddress.id == seed_id).execution_options(synchronize_session=False))
        s.commit()
    return {"ok": True}


class PlacementIn(BaseModel):
    seed_email: str
    placement: str   # inbox|spam|promotions|missing|unknown
    note: str | None = None


@router.post("/batches/{batch_id}/placement")
def log_placement(batch_id: int, body: PlacementIn):
    if body.placement not in ("inbox", "spam", "promotions", "missing", "unknown"):
        raise HTTPException(400, "bad placement")
    with db.session() as s:
        s.execute(
            insert(SeedPlacement)
            .values(batch_id=batch_id, seed_email=body.seed_email.strip().lower(), placement=body.placement,
                    checked_at=func.datetime("now"), note=body.note)
            .execution_options(synchronize_session=False)
        )
        s.commit()
    return {"ok": True}
