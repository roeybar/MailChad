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
from pydantic import BaseModel

from mailchad.terminal import db, settings as _settings
from mailchad.terminal.auth import require_session

log = logging.getLogger("vault.admin")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_session)])


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
    with db.conn() as c:
        if tag:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM contacts WHERE tags LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (f"%{tag}%", limit, offset),
            ).fetchall()]
            total = c.execute(
                "SELECT count(*) AS n FROM contacts WHERE tags LIKE ?", (f"%{tag}%",)
            ).fetchone()["n"]
        else:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM contacts ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()]
            total = c.execute("SELECT count(*) AS n FROM contacts").fetchone()["n"]
    return {"contacts": rows, "total": total}


@router.get("/contacts/count")
def count_contacts(tag: str | None = None):
    with db.conn() as c:
        if tag:
            n = c.execute(
                "SELECT count(*) AS n FROM contacts WHERE tags LIKE ?", (f"%{tag}%",)
            ).fetchone()["n"]
        else:
            n = c.execute("SELECT count(*) AS n FROM contacts").fetchone()["n"]
    return {"count": n, "tag": tag}


@router.post("/contacts")
def create_contact(c_in: ContactIn):
    email = c_in.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "invalid email")

    h = _email_hash(email)
    with db.conn() as c:
        # Refuse insert if hash is in suppression.
        sup = c.execute("SELECT reason FROM suppression_hashes WHERE email_hash = ?", (h,)).fetchone()
        if sup:
            raise HTTPException(409, f"email is in suppression (reason={sup['reason']})")
        try:
            cur = c.execute(
                "INSERT INTO contacts (email, name, tags, consent_ts, consent_source, external_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (email, c_in.name, c_in.tags, _now(), c_in.consent_source, c_in.external_id),
            )
            c.commit()
            new_id = cur.lastrowid
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

    from mailchad.terminal import email_clean
    now = _now()
    with db.conn() as c:
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
            if c.execute("SELECT 1 FROM suppression_hashes WHERE email_hash=?", (h,)).fetchone():
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
                c.execute(
                    "INSERT OR IGNORE INTO contacts (email, name, tags, consent_ts, consent_source, external_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (email,
                     _col(row, "name") or None,
                     csv_tags or None,
                     now,
                     _col(row, "consent_source", "csv_import"),
                     _col(row, "external_id") or None),
                )
                if c.execute("SELECT changes()").fetchone()[0]:
                    imported += 1
            except Exception as exc:
                if len(errors) < 20:
                    errors.append(f"row {i+2}: {exc}")
        c.commit()

    db.audit(actor="operator", action="contacts.csv_import",
             details={"imported": imported, "skipped_suppressed": skipped_suppressed,
                      "skipped_invalid": skipped_invalid, "dropped_reasons": dropped_reasons})
    return {"imported": imported, "skipped_suppressed": skipped_suppressed,
            "skipped_invalid": skipped_invalid, "dropped_reasons": dropped_reasons,
            "errors": errors}


@router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: int):
    with db.conn() as c:
        row = c.execute("SELECT email FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such contact")
        c.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        c.commit()
    db.audit(actor="operator", action="contact.deleted", target=f"contact:{contact_id}",
             details={"email_hash": _email_hash(row["email"])})
    return {"deleted": True}


@router.post("/contacts/bulk-delete")
def bulk_delete_contacts(body: dict):
    ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()]
    if not ids:
        raise HTTPException(400, "no ids provided")
    deleted = 0
    with db.conn() as c:
        for cid in ids:
            r = c.execute("DELETE FROM contacts WHERE id=?", (cid,))
            deleted += r.rowcount
        c.commit()
    db.audit(actor="operator", action="contacts.bulk_deleted", details={"count": deleted})
    return {"deleted": deleted}


@router.get("/contacts/export")
def export_contacts(ids: str | None = None):
    from fastapi.responses import Response
    def _q(v): return '"' + str(v or "").replace('"', '""') + '"'
    with db.conn() as c:
        if ids:
            id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
            ph = ",".join("?" * len(id_list))
            rows = c.execute(
                f"SELECT email,name,tags,consent_source,external_id,created_at FROM contacts WHERE id IN ({ph}) ORDER BY id",
                id_list,
            ).fetchall() if id_list else []
        else:
            rows = c.execute("SELECT email,name,tags,consent_source,external_id,created_at FROM contacts ORDER BY id").fetchall()
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
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM templates ORDER BY updated_at DESC"
        ).fetchall()]
    return {"templates": rows}


@router.post("/templates")
def create_template(t: TemplateIn):
    h = _template_hash(t.subject, t.from_name, t.html_body)
    with db.conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO templates (name, subject, from_name, html_body, text_body, template_hash, tracking_enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (t.name, t.subject, t.from_name, t.html_body, t.text_body, h, int(t.tracking_enabled)),
            )
            c.commit()
            new_id = cur.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "template name already exists")
            raise
    db.audit(actor="operator", action="template.created", target=f"template:{new_id}")
    return {"id": new_id, "template_hash": h}


@router.patch("/templates/{template_id}")
def update_template(template_id: int, t: TemplateIn):
    h = _template_hash(t.subject, t.from_name, t.html_body)
    with db.conn() as c:
        row = c.execute("SELECT id FROM templates WHERE id = ?", (template_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such template")
        c.execute(
            "UPDATE templates SET name=?, subject=?, from_name=?, html_body=?, text_body=?, "
            "template_hash=?, tracking_enabled=?, updated_at=datetime('now') WHERE id=?",
            (t.name, t.subject, t.from_name, t.html_body, t.text_body, h,
             int(t.tracking_enabled), template_id),
        )
        c.commit()
    db.audit(actor="operator", action="template.updated", target=f"template:{template_id}",
             details={"new_hash": h})
    return {"updated": True, "template_hash": h}


# Campaigns

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
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        ).fetchall()]
    return {"campaigns": rows}


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int):
    with db.conn() as c:
        row = c.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such campaign")
        recipients_total = c.execute(
            "SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()["n"]
    return {"campaign": dict(row), "recipients_total": recipients_total}


@router.post("/campaigns")
def create_campaign(c_in: CampaignIn):
    if c_in.kind not in ("promotional", "transactional"):
        raise HTTPException(400, "kind must be 'promotional' or 'transactional'")
    with db.conn() as c:
        tmpl = c.execute("SELECT id FROM templates WHERE id = ?", (c_in.template_id,)).fetchone()
        if not tmpl:
            raise HTTPException(404, "no such template")
        if c_in.entity_id is not None:
            if not c.execute("SELECT id FROM entities WHERE id=?", (c_in.entity_id,)).fetchone():
                raise HTTPException(404, "no such company")
        initial_status = "scheduled" if c_in.scheduled_for else "draft"
        cur = c.execute(
            "INSERT INTO campaigns (name, template_id, kind, entity_id, scheduled_for, status, rate_limit_per_min, bounce_pause_pct, human_send, human_send_min_s, human_send_max_s, human_send_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c_in.name, c_in.template_id, c_in.kind, c_in.entity_id, c_in.scheduled_for, initial_status,
             c_in.rate_limit_per_min, c_in.bounce_pause_pct,
             int(c_in.human_send), c_in.human_send_min_s, c_in.human_send_max_s, c_in.human_send_count),
        )
        c.commit()
        new_id = cur.lastrowid
    db.audit(actor="operator", action="campaign.created", target=f"campaign:{new_id}")
    return {"id": new_id}


class AddRecipientsIn(BaseModel):
    contact_ids: list[int] | None = None
    by_tag: str | None = None      # match contacts whose tags contain this token


@router.post("/campaigns/{campaign_id}/recipients")
def add_recipients(campaign_id: int, body: AddRecipientsIn):
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone():
            raise HTTPException(404, "no such campaign")

        ids = set(body.contact_ids or [])
        if body.by_tag:
            tag = body.by_tag.strip()
            for row in c.execute("SELECT id, tags FROM contacts WHERE tags IS NOT NULL").fetchall():
                if tag in (row["tags"] or "").split("|"):
                    ids.add(row["id"])

        added = 0
        for cid in ids:
            try:
                c.execute(
                    "INSERT INTO campaign_recipients (campaign_id, contact_id) VALUES (?, ?)",
                    (campaign_id, cid),
                )
                added += 1
            except Exception as e:
                if "UNIQUE" in str(e):
                    continue
                raise
        c.execute(
            "UPDATE campaigns SET recipient_count = (SELECT count(*) FROM campaign_recipients WHERE campaign_id=?), "
            "updated_at=datetime('now') WHERE id=?",
            (campaign_id, campaign_id),
        )
        c.commit()
    db.audit(actor="operator", action="campaign.recipients_added",
             target=f"campaign:{campaign_id}", details={"added": added})
    return {"added": added, "total_in_set": len(ids)}


class MarkTestedIn(BaseModel):
    """Operator confirms they sent + received a test, ready to launch."""
    pass


@router.post("/campaigns/{campaign_id}/mark-tested")
def mark_tested(campaign_id: int):
    """Set test_sent_at + capture template_hash_at_test (v1.7.6 gate)."""
    with db.conn() as c:
        camp = c.execute("SELECT template_id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not camp:
            raise HTTPException(404, "no such campaign")
        tmpl = c.execute("SELECT template_hash FROM templates WHERE id = ?", (camp["template_id"],)).fetchone()
        c.execute(
            "UPDATE campaigns SET status='tested', test_sent_at=datetime('now'), "
            "template_hash_at_test=?, updated_at=datetime('now') WHERE id=?",
            (tmpl["template_hash"], campaign_id),
        )
        c.commit()
    db.audit(actor="operator", action="campaign.tested", target=f"campaign:{campaign_id}",
             details={"template_hash": tmpl["template_hash"]})
    return {"tested": True, "template_hash_at_test": tmpl["template_hash"]}


# Suppression (manual + read-only list)

class SuppressionAddIn(BaseModel):
    email: str
    reason: str = "manual"


@router.post("/suppression")
def add_suppression(body: SuppressionAddIn):
    if body.reason not in ("unsubscribe", "complaint", "bounce_hard", "manual", "erasure_request"):
        raise HTTPException(400, "bad reason")
    h = _email_hash(body.email)
    with db.conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO suppression_hashes (email_hash, reason, source) VALUES (?, ?, ?)",
            (h, body.reason, "manual:admin"),
        )
        c.commit()
    db.audit(actor="operator", action="suppression.added", target=f"hash:{h}",
             details={"reason": body.reason})
    return {"email_hash": h, "reason": body.reason}


@router.get("/suppression")
def list_suppression(limit: int = 500):
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT email_hash, reason, added_at, source FROM suppression_hashes ORDER BY added_at DESC LIMIT ?",
            (limit,),
        ).fetchall()]
        total = c.execute("SELECT count(*) AS n FROM suppression_hashes").fetchone()["n"]
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
    with db.conn() as c:
        rows = [_entity_row(dict(r)) for r in c.execute(
            "SELECT * FROM entities ORDER BY name"
        ).fetchall()]
    return {"entities": rows}


@router.get("/entities/{entity_id}")
def get_entity(entity_id: int):
    with db.conn() as c:
        row = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such company")
    return _entity_row(dict(row))


@router.post("/entities")
def create_entity(body: EntityIn):
    try:
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO entities (name, domain, from_name, from_email, footer_address, support_email, public_host) VALUES (?,?,?,?,?,?,?)",
                (body.name, body.domain, body.from_name, body.from_email,
                 body.footer_address, body.support_email, body.public_host),
            )
            c.commit()
            new_id = cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "entity name already exists")
        raise
    db.audit(actor="operator", action="entity.created", target=f"entity:{new_id}")
    return {"id": new_id}


@router.patch("/entities/{entity_id}")
def update_entity(entity_id: int, body: EntityIn):
    with db.conn() as c:
        if not c.execute("SELECT id FROM entities WHERE id=?", (entity_id,)).fetchone():
            raise HTTPException(404, "no such entity")
        c.execute(
            "UPDATE entities SET name=?, domain=?, from_name=?, from_email=?, "
            "footer_address=?, support_email=?, public_host=?, updated_at=datetime('now') WHERE id=?",
            (body.name, body.domain, body.from_name, body.from_email,
             body.footer_address, body.support_email, body.public_host, entity_id),
        )
        c.commit()
    db.audit(actor="operator", action="entity.updated", target=f"entity:{entity_id}")
    return {"updated": True}


@router.post("/entities/{entity_id}/set-key")
def set_entity_key(entity_id: int, body: EntityKeyIn):
    with db.conn() as c:
        if not c.execute("SELECT id FROM entities WHERE id=?", (entity_id,)).fetchone():
            raise HTTPException(404, "no such company")
        enc = _settings._encrypt(body.resend_api_key)
        c.execute(
            "UPDATE entities SET resend_key_enc=?, updated_at=datetime('now') WHERE id=?",
            (enc, entity_id),
        )
        c.commit()
    db.audit(actor="operator", action="entity.key_set", target=f"company:{entity_id}")
    return {"key_set": True}


@router.delete("/entities/{entity_id}/key")
def clear_entity_key(entity_id: int):
    with db.conn() as c:
        if not c.execute("SELECT id FROM entities WHERE id=?", (entity_id,)).fetchone():
            raise HTTPException(404, "no such company")
        c.execute(
            "UPDATE entities SET resend_key_enc=NULL, updated_at=datetime('now') WHERE id=?",
            (entity_id,),
        )
        c.commit()
    db.audit(actor="operator", action="company.key_cleared", target=f"company:{entity_id}")
    return {"key_cleared": True}


@router.delete("/entities/{entity_id}")
def delete_company(entity_id: int):
    with db.conn() as c:
        if not c.execute("SELECT id FROM entities WHERE id=?", (entity_id,)).fetchone():
            raise HTTPException(404, "no such company")
        # disassociate campaigns rather than blocking delete
        c.execute("UPDATE campaigns SET entity_id=NULL WHERE entity_id=?", (entity_id,))
        c.execute("DELETE FROM entities WHERE id=?", (entity_id,))
        c.commit()
    db.audit(actor="operator", action="entity.deleted", target=f"company:{entity_id}")
    return {"deleted": True}


# Campaign Analytics

@router.get("/campaigns/{campaign_id}/analytics/data")
def campaign_analytics_data(campaign_id: int):
    with db.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            raise HTTPException(404, "no such campaign")

        total = c.execute(
            "SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id=?", (campaign_id,)
        ).fetchone()["n"]

        def _count(status_col: str) -> int:
            return c.execute(
                f"SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id=? AND {status_col} IS NOT NULL",
                (campaign_id,),
            ).fetchone()["n"]

        sent      = c.execute("SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id=? AND status IN ('sent','delivered','opened','clicked')", (campaign_id,)).fetchone()["n"]
        failed    = c.execute("SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id=? AND status='failed'", (campaign_id,)).fetchone()["n"]
        queued    = c.execute("SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id=? AND status='queued'", (campaign_id,)).fetchone()["n"]
        opened    = _count("opened_at")
        clicked   = _count("clicked_at")
        bounced   = _count("bounced_at")
        complained = _count("complained_at")

        rows = [dict(r) for r in c.execute(
            "SELECT cr.id, cr.contact_id, cr.status, cr.message_id, cr.sent_at, cr.opened_at, "
            "cr.clicked_at, cr.bounced_at, cr.complained_at, cr.failure_reason, "
            "con.email, con.name "
            "FROM campaign_recipients cr "
            "LEFT JOIN contacts con ON con.id = cr.contact_id "
            "WHERE cr.campaign_id = ? ORDER BY cr.id",
            (campaign_id,),
        ).fetchall()]

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
    with db.conn() as c:
        src = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not src:
            raise HTTPException(404, "no such campaign")
        src = dict(src)
        cur = c.execute(
            "INSERT INTO campaigns (name, template_id, kind, entity_id, rate_limit_per_min, "
            "bounce_pause_pct, human_send, human_send_min_s, human_send_max_s, human_send_count, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')",
            (f"{src['name']} (copy)", src["template_id"], src["kind"], src.get("entity_id"),
             src.get("rate_limit_per_min"), src.get("bounce_pause_pct") or 0.10,
             src.get("human_send", 0), src.get("human_send_min_s", 60),
             src.get("human_send_max_s", 210), src.get("human_send_count", 1)),
        )
        c.commit()
        new_id = cur.lastrowid
    db.audit(actor="operator", action="campaign.cloned", target=f"campaign:{new_id}",
             details={"source": campaign_id})
    return {"id": new_id}


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: int):
    """Pause a scheduled or dispatched campaign. In-flight SQS packs are unaffected."""
    with db.conn() as c:
        camp = c.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            raise HTTPException(404, "no such campaign")
        if camp["status"] not in ("scheduled", "dispatched", "tested"):
            raise HTTPException(400, f"cannot pause campaign with status {camp['status']!r}")
        c.execute("UPDATE campaigns SET status='paused', updated_at=datetime('now') WHERE id=?",
                  (campaign_id,))
        c.commit()
    db.audit(actor="operator", action="campaign.paused", target=f"campaign:{campaign_id}")
    return {"paused": True}


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: int):
    """Resume a paused campaign back to its prior actionable state."""
    with db.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            raise HTTPException(404, "no such campaign")
        if camp["status"] != "paused":
            raise HTTPException(400, "campaign is not paused")
        resume_status = "tested" if camp["test_sent_at"] else "draft"
        c.execute("UPDATE campaigns SET status=?, updated_at=datetime('now') WHERE id=?",
                  (resume_status, campaign_id))
        c.commit()
    db.audit(actor="operator", action="campaign.resumed", target=f"campaign:{campaign_id}")
    return {"resumed": True, "status": resume_status}


@router.post("/campaigns/{campaign_id}/retry")
def retry_failed(campaign_id: int):
    """Reset failed recipients to 'queued' and set campaign back to 'tested' so it can relaunch."""
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
            raise HTTPException(404, "no such campaign")
        result = c.execute(
            "UPDATE campaign_recipients SET status='queued', failure_reason=NULL WHERE campaign_id=? AND status='failed'",
            (campaign_id,),
        )
        reset_count = result.rowcount
        if reset_count:
            c.execute(
                "UPDATE campaigns SET status='tested', updated_at=datetime('now') WHERE id=?",
                (campaign_id,),
            )
        c.commit()
    db.audit(actor="operator", action="campaign.retry_failed",
             target=f"campaign:{campaign_id}", details={"reset": reset_count})
    return {"reset": reset_count}


# Operators (Feature 10)

class OperatorIn(BaseModel):
    email: str
    password: str
    role: str = "operator"


@router.get("/operators")
def list_operators():
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, email, role, active, created_at, last_login_at FROM operators ORDER BY id"
        ).fetchall()]
    return {"operators": rows}


@router.post("/operators")
def create_operator(body: OperatorIn, session=Depends(require_session)):
    from mailchad.terminal.auth import require_admin
    import bcrypt as _bcrypt
    if body.role not in ("admin", "operator"):
        raise HTTPException(400, "role must be admin or operator")
    h = _bcrypt.hashpw(body.password.encode(), _bcrypt.gensalt(rounds=12)).decode()
    with db.conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO operators (email, password_hash, role) VALUES (?, ?, ?)",
                (body.email.strip().lower(), h, body.role),
            )
            c.commit()
            new_id = cur.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "email already exists")
            raise
    db.audit(actor="operator", action="operator.created", target=f"operator:{new_id}")
    return {"id": new_id}


@router.post("/operators/{op_id}/toggle-active")
def toggle_operator_active(op_id: int):
    with db.conn() as c:
        row = c.execute("SELECT active FROM operators WHERE id=?", (op_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such operator")
        new_state = 0 if row["active"] else 1
        c.execute("UPDATE operators SET active=? WHERE id=?", (new_state, op_id))
        c.commit()
    db.audit(actor="operator", action="operator.toggled", target=f"operator:{op_id}",
             details={"active": new_state})
    return {"active": new_state}


@router.delete("/operators/{op_id}")
def delete_operator(op_id: int, session=Depends(require_session)):
    with db.conn() as c:
        if not c.execute("SELECT id FROM operators WHERE id=?", (op_id,)).fetchone():
            raise HTTPException(404, "no such operator")
        c.execute("DELETE FROM operators WHERE id=?", (op_id,))
        c.commit()
    db.audit(actor="operator", action="operator.deleted", target=f"operator:{op_id}")
    return {"deleted": True}


def get_entity_credentials(entity_id: int) -> dict:
    """Used by launch.py - returns {resend_key, from_email, from_name, domain} or raises."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
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
    with db.conn() as c:
        row = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
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
            "detail": f"Key valid. Domain '{row['domain']}' {'✓ verified' if domain_ok else '✗ not found in Resend account'} among {len(domains)} domain(s).",
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

    resend_key = _settings.get("resend_api_key", "")
    from_addr = _settings.get("email_from", "") or "noreply@example.com"
    from_name_val = body.from_name
    from_email_val = body.from_email

    if body.entity_id:
        with db.conn() as c:
            row = c.execute("SELECT * FROM entities WHERE id=?", (body.entity_id,)).fetchone()
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
    with db.conn() as c:
        scheduled = [dict(r) for r in c.execute(
            "SELECT c.*, t.name AS template_name, e.name AS entity_name "
            "FROM campaigns c LEFT JOIN templates t ON t.id=c.template_id "
            "LEFT JOIN entities e ON e.id=c.entity_id "
            "WHERE c.status='scheduled' ORDER BY c.scheduled_for ASC"
        ).fetchall()]
        active = [dict(r) for r in c.execute(
            "SELECT c.*, t.name AS template_name, e.name AS entity_name, "
            "  (SELECT count(*) FROM campaign_recipients WHERE campaign_id=c.id AND status='queued') AS queued_count, "
            "  (SELECT count(*) FROM campaign_recipients WHERE campaign_id=c.id AND status IN ('sent','opened','clicked')) AS sent_count "
            "FROM campaigns c LEFT JOIN templates t ON t.id=c.template_id "
            "LEFT JOIN entities e ON e.id=c.entity_id "
            "WHERE c.status='dispatched' ORDER BY c.dispatched_at DESC LIMIT 10"
        ).fetchall()]
        paused = [dict(r) for r in c.execute(
            "SELECT c.*, t.name AS template_name, e.name AS entity_name "
            "FROM campaigns c LEFT JOIN templates t ON t.id=c.template_id "
            "LEFT JOIN entities e ON e.id=c.entity_id "
            "WHERE c.status='paused' ORDER BY c.updated_at DESC"
        ).fetchall()]
        recent = [dict(r) for r in c.execute(
            "SELECT c.*, t.name AS template_name, e.name AS entity_name "
            "FROM campaigns c LEFT JOIN templates t ON t.id=c.template_id "
            "LEFT JOIN entities e ON e.id=c.entity_id "
            "WHERE c.status='dispatched' AND c.dispatched_at >= datetime('now','-7 days') "
            "ORDER BY c.dispatched_at DESC LIMIT 20"
        ).fetchall()]
    return {"scheduled": scheduled, "active": active, "paused": paused, "recent": recent}


# Campaign stages (multi-stage / drip)

class StageIn(BaseModel):
    template_id: int
    scheduled_for: str   # ISO datetime
    note: str | None = None


@router.get("/campaigns/{campaign_id}/stages")
def list_stages(campaign_id: int):
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
            raise HTTPException(404, "no such campaign")
        rows = [dict(r) for r in c.execute(
            "SELECT cs.*, t.name AS template_name "
            "FROM campaign_stages cs JOIN templates t ON t.id=cs.template_id "
            "WHERE cs.campaign_id=? ORDER BY cs.stage_number",
            (campaign_id,),
        ).fetchall()]
    return {"stages": rows}


@router.post("/campaigns/{campaign_id}/stages")
def add_stage(campaign_id: int, body: StageIn):
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
            raise HTTPException(404, "no such campaign")
        if not c.execute("SELECT id FROM templates WHERE id=?", (body.template_id,)).fetchone():
            raise HTTPException(404, "no such template")
        next_num = (c.execute("SELECT coalesce(max(stage_number),0)+1 FROM campaign_stages WHERE campaign_id=?",
                              (campaign_id,)).fetchone()[0])
        cur = c.execute(
            "INSERT INTO campaign_stages (campaign_id, stage_number, template_id, scheduled_for, note) VALUES (?,?,?,?,?)",
            (campaign_id, next_num, body.template_id, body.scheduled_for, body.note),
        )
        c.commit()
        new_id = cur.lastrowid
    db.audit(actor="operator", action="campaign.stage_added",
             target=f"campaign:{campaign_id}", details={"stage_id": new_id})
    return {"id": new_id, "stage_number": next_num}


@router.patch("/campaigns/{campaign_id}/stages/{stage_id}")
def update_stage(campaign_id: int, stage_id: int, body: StageIn):
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaign_stages WHERE id=? AND campaign_id=?",
                         (stage_id, campaign_id)).fetchone():
            raise HTTPException(404, "no such stage")
        c.execute(
            "UPDATE campaign_stages SET template_id=?, scheduled_for=?, note=? WHERE id=?",
            (body.template_id, body.scheduled_for, body.note, stage_id),
        )
        c.commit()
    return {"updated": True}


@router.delete("/campaigns/{campaign_id}/stages/{stage_id}")
def delete_stage(campaign_id: int, stage_id: int):
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaign_stages WHERE id=? AND campaign_id=?",
                         (stage_id, campaign_id)).fetchone():
            raise HTTPException(404, "no such stage")
        c.execute("DELETE FROM campaign_stages WHERE id=?", (stage_id,))
        # Renumber remaining stages
        remaining = c.execute(
            "SELECT id FROM campaign_stages WHERE campaign_id=? ORDER BY stage_number",
            (campaign_id,)
        ).fetchall()
        for i, r in enumerate(remaining, 1):
            c.execute("UPDATE campaign_stages SET stage_number=? WHERE id=?", (i, r["id"]))
        c.commit()
    return {"deleted": True}


# Selective template export / bulk import

@router.get("/templates/export")
def export_templates(ids: str | None = None):
    from fastapi.responses import Response
    with db.conn() as c:
        if ids:
            id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
            ph = ",".join("?" * len(id_list))
            rows = c.execute(
                f"SELECT name,subject,from_name,html_body,text_body,tracking_enabled FROM templates WHERE id IN ({ph}) ORDER BY id",
                id_list,
            ).fetchall() if id_list else []
        else:
            rows = c.execute("SELECT name,subject,from_name,html_body,text_body,tracking_enabled FROM templates ORDER BY id").fetchall()
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
    with db.conn() as c:
        if ids:
            id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
            ph = ",".join("?" * len(id_list))
            rows = c.execute(
                f"SELECT name,domain,from_name,from_email,footer_address,support_email,public_host FROM entities WHERE id IN ({ph}) ORDER BY id",
                id_list,
            ).fetchall() if id_list else []
        else:
            rows = c.execute("SELECT name,domain,from_name,from_email,footer_address,support_email,public_host FROM entities ORDER BY id").fetchall()
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
    with db.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            raise HTTPException(404, "no such campaign")
        camp = dict(camp)
        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.entity_id is not None:
            if not c.execute("SELECT id FROM entities WHERE id=?", (body.entity_id,)).fetchone():
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
            updates["scheduled_for"] = body.scheduled_for
        if not updates:
            return {"updated": False, "reason": "no fields provided"}
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [campaign_id]
        c.execute(
            f"UPDATE campaigns SET {set_clause}, updated_at=datetime('now') WHERE id=?",
            vals,
        )
        c.commit()
    db.audit(actor="operator", action="campaign.edited", target=f"campaign:{campaign_id}",
             details={"fields": list(updates.keys())})
    return {"updated": True}


# Contact edit

class ContactEditIn(BaseModel):
    name: str | None = None
    tags: str | None = None


@router.patch("/contacts/{contact_id}")
def edit_contact(contact_id: int, body: ContactEditIn):
    with db.conn() as c:
        row = c.execute("SELECT id FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such contact")
        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.tags is not None:
            updates["tags"] = body.tags
        if not updates:
            return {"updated": False, "reason": "no fields provided"}
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [contact_id]
        c.execute(f"UPDATE contacts SET {set_clause} WHERE id=?", vals)
        c.commit()
    db.audit(actor="operator", action="contact.edited", target=f"contact:{contact_id}",
             details={"fields": list(updates.keys())})
    return {"updated": True}


# Template delete

@router.delete("/templates/{template_id}")
def delete_template(template_id: int):
    with db.conn() as c:
        if not c.execute("SELECT id FROM templates WHERE id=?", (template_id,)).fetchone():
            raise HTTPException(404, "no such template")
        in_use = c.execute(
            "SELECT id FROM campaigns WHERE template_id=? LIMIT 1", (template_id,)
        ).fetchone()
        if in_use:
            raise HTTPException(409, "template is referenced by one or more campaigns")
        c.execute("DELETE FROM templates WHERE id=?", (template_id,))
        c.commit()
    db.audit(actor="operator", action="template.deleted", target=f"template:{template_id}")
    return {"deleted": True}


# Campaign archive

@router.post("/campaigns/{campaign_id}/archive")
def archive_campaign(campaign_id: int):
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
            raise HTTPException(404, "no such campaign")
        c.execute(
            "UPDATE campaigns SET status='archived', updated_at=datetime('now') WHERE id=?",
            (campaign_id,),
        )
        c.commit()
    db.audit(actor="operator", action="campaign.archived", target=f"campaign:{campaign_id}")
    return {"archived": True}


# Add contacts to campaign

class AddToCampaignIn(BaseModel):
    campaign_id: int
    contact_ids: list[int]


@router.post("/contacts/add-to-campaign")
def add_contacts_to_campaign(body: AddToCampaignIn):
    with db.conn() as c:
        if not c.execute("SELECT id FROM campaigns WHERE id=?", (body.campaign_id,)).fetchone():
            raise HTTPException(404, "no such campaign")
        added = 0
        for cid in body.contact_ids:
            c.execute(
                "INSERT OR IGNORE INTO campaign_recipients (campaign_id, contact_id) VALUES (?, ?)",
                (body.campaign_id, cid),
            )
            if c.execute("SELECT changes()").fetchone()[0]:
                added += 1
        c.execute(
            "UPDATE campaigns SET recipient_count = (SELECT count(*) FROM campaign_recipients WHERE campaign_id=?), "
            "updated_at=datetime('now') WHERE id=?",
            (body.campaign_id, body.campaign_id),
        )
        c.commit()
        total = c.execute(
            "SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id=?",
            (body.campaign_id,),
        ).fetchone()["n"]
    db.audit(actor="operator", action="campaign.recipients_added",
             target=f"campaign:{body.campaign_id}", details={"added": added})
    return {"added": added, "total": total}


# Tags

@router.get("/tags")
def list_tags():
    return {"tags": list_tags_data()}


def list_tags_data() -> list:
    counts: dict[str, int] = {}
    with db.conn() as c:
        for row in c.execute("SELECT tags FROM contacts WHERE tags IS NOT NULL").fetchall():
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
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, tags FROM contacts WHERE tags LIKE ?", (f"%{old}%",)
        ).fetchall()
        for row in rows:
            tags = (row["tags"] or "").split("|")
            new_tags = [new if t.strip() == old else t for t in tags]
            if new_tags != tags:
                c.execute("UPDATE contacts SET tags=? WHERE id=?",
                          ("|".join(new_tags), row["id"]))
                updated += 1
        c.commit()
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
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, tags FROM contacts WHERE tags LIKE ?", (f"%{tag}%",)
        ).fetchall()
        for row in rows:
            tags = [t for t in (row["tags"] or "").split("|") if t.strip() != tag]
            new_tags_str = "|".join(tags) if tags else None
            c.execute("UPDATE contacts SET tags=? WHERE id=?", (new_tags_str, row["id"]))
            updated += 1
        c.commit()
    db.audit(actor="operator", action="tags.deleted",
             details={"tag": tag, "updated": updated})
    return {"updated": updated}


# Campaign test-send-chain checkpoint

@router.get("/campaigns/{campaign_id}/test-send-chain")
def test_send_chain(campaign_id: int):
    from mailchad.terminal import encryption
    result: dict = {
        "template_ok":  False, "template_error": None,
        "k_temp_ok":    False, "k_temp_error":   None,
        "cloud_ok":     False, "cloud_error":    None,
        "entity_ok":    False, "entity_error":   None,
        "unsub_ok":     False, "unsub_error":    None,
        "tracking_ok":  True,  "tracking_note":  "click_tracking on (body buttons); opens NOT tracked (bot noise)",
    }

    with db.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            raise HTTPException(404, "no such campaign")
        camp = dict(camp)

        # (a) Template check
        tmpl = c.execute(
            "SELECT html_body FROM templates WHERE id=?", (camp["template_id"],)
        ).fetchone()
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
            ent = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
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
    from mailchad.terminal import settings as _s
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
    """Render the stored template and fire a real test email via the entity's Resend key."""
    import httpx as _httpx
    from mailchad.terminal.launch import _render, _inject_compliance
    from mailchad.terminal import settings as _settings

    if "@" not in body.to_email:
        raise HTTPException(400, "invalid email")

    with db.conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not camp:
            raise HTTPException(404, "no such campaign")
        camp = dict(camp)
        tmpl = c.execute("SELECT * FROM templates WHERE id=?", (camp["template_id"],)).fetchone()
        if not tmpl:
            raise HTTPException(404, "template not found")
        tmpl = dict(tmpl)

        resend_key = _settings.get("resend_api_key", "") or ""
        from_addr  = _settings.get("email_from", "noreply@example.com") or "noreply@example.com"

        entity_id = camp.get("entity_id")
        if entity_id:
            ent = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
            if ent and ent["resend_key_enc"]:
                resend_key = _settings._decrypt(ent["resend_key_enc"])
            if ent:
                fn = ent["from_name"] or ent["name"]
                fe = ent["from_email"] or f"hello@{ent['domain']}"
                from_addr = f"{fn} <{fe}>" if fn else fe

    if not resend_key:
        raise HTTPException(400, "No Resend API key on entity or global settings")

    test_contact = {"email": body.to_email, "name": body.to_email.split("@")[0]}
    subject, html, text = _render(tmpl, test_contact)

    public_host = _settings.get("public_host", "example.com") or "example.com"
    html, text = _inject_compliance(html, text,
                                    f"https://{public_host}/u/test-preview",
                                    f"https://{public_host}/e/test-preview",
                                    camp["kind"])
    try:
        r = _httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}"},
            json={"from": from_addr, "to": [body.to_email],
                  "subject": f"[Test] {subject}", "html": html, "text": text,
                  "open_tracking": False, "click_tracking": True},
            timeout=15,
        )
        if r.status_code >= 400:
            raise HTTPException(400, f"Resend rejected: {r.text[:300]}")
        return {"sent": True, "message_id": r.json().get("id"), "to": body.to_email}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"send failed: {e}")


@router.post("/campaigns/{campaign_id}/relaunch")
async def relaunch_campaign(campaign_id: int):
    """Re-launch a dispatched/failed campaign to all recipients (including previously failed)."""
    from mailchad.terminal import launch as _launch
    try:
        return await _launch.launch_campaign(campaign_id,
                                             actor="operator:relaunch",
                                             relaunch=True)
    except _launch.LaunchError as e:
        raise HTTPException(400, str(e))
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
    from mailchad.terminal import encryption
    cloud = os.environ.get("CLOUD_URL", "")
    bearer_path = encryption.KEYS_DIR / "cloud_bearer.txt"
    bearer = bearer_path.read_text().strip() if bearer_path.exists() else ""
    # Cancel by campaign_id on the cloud - catches ALL pending packs including salted
    # seed packs (which have no dispatched_job row, so per-pack deletion would miss them).
    cancelled = 0
    if cloud and bearer:
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                r = await client.post(f"{cloud}/packs/cancel-campaign",
                                      json={"campaign_id": campaign_id},
                                      headers={"Authorization": f"Bearer {bearer}"})
                if r.status_code == 200:
                    cancelled = r.json().get("cancelled", 0)
            except Exception:
                pass
    with db.conn() as c:
        c.execute("UPDATE campaigns SET status='cancelled', updated_at=datetime('now') WHERE id=?",
                  (campaign_id,))
        c.execute("UPDATE campaign_batches SET status='cancelled' "
                  "WHERE campaign_id=? AND status IN ('pending','scheduled','sending')",
                  (campaign_id,))
        c.commit()
    db.audit(actor="operator", action="campaign.cancelled",
             target=f"campaign:{campaign_id}", details={"packs_cancelled": cancelled})
    return {"ok": True, "packs_cancelled": cancelled}


# v3.22 batches + approve-next

@router.get("/campaigns/{campaign_id}/batches")
def list_batches(campaign_id: int):
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM campaign_batches WHERE campaign_id=? ORDER BY batch_no",
            (campaign_id,),
        ).fetchall()]
    # which batch is next-approvable?
    nxt = next((b for b in rows if b["status"] == "pending"), None)
    return {"batches": rows, "next_pending": nxt["batch_no"] if nxt else None}


@router.post("/campaigns/{campaign_id}/batches/{batch_no}/approve")
async def approve_batch(campaign_id: int, batch_no: int):
    """Manually release the next pending batch - gated by the analytics cooldown
    (approve_unlock_at, default = next day's window)."""
    from mailchad.terminal import launch as _launch
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.conn() as c:
        b = c.execute("SELECT * FROM campaign_batches WHERE campaign_id=? AND batch_no=?",
                      (campaign_id, batch_no)).fetchone()
        if not b:
            raise HTTPException(404, "no such batch")
        b = dict(b)
        if b["status"] != "pending":
            raise HTTPException(400, f"batch {batch_no} is '{b['status']}', not pending")
        # the previous batch must be drained + cooldown elapsed
        prev = c.execute("SELECT * FROM campaign_batches WHERE campaign_id=? AND batch_no=?",
                         (campaign_id, batch_no - 1)).fetchone()
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
    with db.conn() as c:
        c.execute("UPDATE campaign_batches SET approved_at=datetime('now') "
                  "WHERE campaign_id=? AND batch_no=?", (campaign_id, batch_no))
        if prev:
            c.execute("UPDATE campaign_batches SET status='done' "
                      "WHERE campaign_id=? AND batch_no=?", (campaign_id, batch_no - 1))
        c.commit()
    db.audit(actor="operator", action="batch.approved",
             target=f"campaign:{campaign_id}:batch:{batch_no}", details=result)
    return result


# Seed addresses (inbox-placement monitors)

class SeedIn(BaseModel):
    email: str
    provider: str | None = None


@router.get("/seeds")
def list_seeds():
    with db.conn() as c:
        return {"seeds": [dict(r) for r in c.execute(
            "SELECT * FROM seed_addresses ORDER BY id").fetchall()]}


@router.post("/seeds")
def add_seed(body: SeedIn):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "invalid email")
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO seed_addresses (email, provider) VALUES (?,?)",
                  (email, body.provider))
        c.commit()
        row = c.execute("SELECT * FROM seed_addresses WHERE email=?", (email,)).fetchone()
    return dict(row)


@router.post("/seeds/{seed_id}/toggle")
def toggle_seed(seed_id: int):
    with db.conn() as c:
        c.execute("UPDATE seed_addresses SET active = 1 - active WHERE id=?", (seed_id,))
        c.commit()
        row = c.execute("SELECT * FROM seed_addresses WHERE id=?", (seed_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such seed")
    return dict(row)


@router.delete("/seeds/{seed_id}")
def delete_seed(seed_id: int):
    with db.conn() as c:
        c.execute("DELETE FROM seed_addresses WHERE id=?", (seed_id,))
        c.commit()
    return {"ok": True}


class PlacementIn(BaseModel):
    seed_email: str
    placement: str   # inbox|spam|promotions|missing|unknown
    note: str | None = None


@router.post("/batches/{batch_id}/placement")
def log_placement(batch_id: int, body: PlacementIn):
    if body.placement not in ("inbox", "spam", "promotions", "missing", "unknown"):
        raise HTTPException(400, "bad placement")
    with db.conn() as c:
        c.execute(
            "INSERT INTO seed_placements (batch_id, seed_email, placement, checked_at, note) "
            "VALUES (?,?,?,datetime('now'),?)",
            (batch_id, body.seed_email.strip().lower(), body.placement, body.note),
        )
        c.commit()
    return {"ok": True}
