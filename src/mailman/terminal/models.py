"""SQLAlchemy ORM models of the terminal SQLite schema.

One class per table of terminal/migrations/0001-0019 plus _migration. The
migrations stay the schema source: product code never calls create_all.
"""
from __future__ import annotations

from sqlalchemy import (BLOB, INTEGER, REAL, TEXT, CheckConstraint, ForeignKey, Index, UniqueConstraint,
                        text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def cols(M) -> tuple:
    """The mapped column attributes of model M, in table order (for `select(*cols(M))`)."""
    return tuple(getattr(M, c.key) for c in M.__table__.columns)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("idx_contacts_email", "email"),
        Index("idx_contacts_external_id", "external_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    email: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    tags: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    consent_ts: Mapped[str] = mapped_column(TEXT, nullable=False)
    consent_source: Mapped[str] = mapped_column(TEXT, nullable=False)
    external_id: Mapped[str | None] = mapped_column(TEXT, nullable=True, unique=True)
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    updated_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))

    campaign_recipients: Mapped[list[CampaignRecipient]] = relationship(
        back_populates="contact", passive_deletes=True)
    dispatched_job: Mapped[list[DispatchedJob]] = relationship(
        back_populates="contact", passive_deletes=True)


class Template(Base):
    __tablename__ = "templates"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    name: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)
    subject: Mapped[str] = mapped_column(TEXT, nullable=False)
    from_name: Mapped[str] = mapped_column(TEXT, nullable=False)
    html_body: Mapped[str] = mapped_column(TEXT, nullable=False)
    text_body: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    template_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    tracking_enabled: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    updated_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))

    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="template", passive_deletes=True)
    campaign_stages: Mapped[list[CampaignStage]] = relationship(
        back_populates="template", passive_deletes=True)


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("idx_campaigns_status", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    name: Mapped[str] = mapped_column(TEXT, nullable=False)
    template_id: Mapped[int] = mapped_column(INTEGER, ForeignKey("templates.id"), nullable=False)
    kind: Mapped[str] = mapped_column(
        TEXT, CheckConstraint("kind IN ('transactional', 'promotional')"), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(
        INTEGER, ForeignKey("entities.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(
        TEXT,
        CheckConstraint("status IN ('draft','tested','scheduled','dispatched', "
                        "'sending','sent','cancelled','paused','failed','archived')"),
        nullable=False, server_default=text("'draft'"))
    test_sent_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    template_hash_at_test: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    scheduled_for: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    dispatched_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    sent_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    recipient_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    updated_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    rate_limit_per_min: Mapped[int | None] = mapped_column(INTEGER, nullable=True, server_default=text("NULL"))
    bounce_pause_pct: Mapped[float | None] = mapped_column(REAL, nullable=True, server_default=text("0.10"))
    human_send: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    human_send_min_s: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("60"))
    human_send_max_s: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("210"))
    human_send_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("1"))
    lock_until: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    unsubs_confirmed_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    lock_duration_s: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("86400"))
    relaunch_gen: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    relaunch_open: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    relaunch_seq: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))

    template: Mapped[Template] = relationship(
        back_populates="campaigns", passive_deletes=True)
    entity: Mapped[Entity | None] = relationship(
        back_populates="campaigns", passive_deletes=True)
    campaign_recipients: Mapped[list[CampaignRecipient]] = relationship(
        back_populates="campaign", passive_deletes=True)
    dispatched_job: Mapped[list[DispatchedJob]] = relationship(
        back_populates="campaign", passive_deletes=True)
    campaign_stages: Mapped[list[CampaignStage]] = relationship(
        back_populates="campaign", passive_deletes=True)


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id"),
        Index("idx_campaign_recipients_message", "message_id"),
        Index("idx_campaign_recipients_campaign", "campaign_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    campaign_id: Mapped[int] = mapped_column(
        INTEGER, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[int] = mapped_column(INTEGER, ForeignKey("contacts.id"), nullable=False)
    message_id: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    status: Mapped[str] = mapped_column(
        TEXT,
        CheckConstraint("status IN ('queued', 'sent', 'delivered', 'opened', 'clicked', 'bounced', "
                        "'complained', 'failed', 'cancelled')"),
        nullable=False, server_default=text("'queued'"))
    sent_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    delivered_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    opened_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    clicked_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    bounced_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    complained_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    batch_no: Mapped[int | None] = mapped_column(INTEGER, nullable=True)
    is_seed: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))

    campaign: Mapped[Campaign] = relationship(
        back_populates="campaign_recipients", passive_deletes=True)
    contact: Mapped[Contact] = relationship(
        back_populates="campaign_recipients", passive_deletes=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("idx_events_message", "message_id"),
        Index("idx_events_type", "event_type"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    event_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    message_id: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    recipient: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    payload_json: Mapped[str] = mapped_column(TEXT, nullable=False)
    received_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    synced_from_edge_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class SuppressionHash(Base):
    __tablename__ = "suppression_hashes"

    email_hash: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    reason: Mapped[str] = mapped_column(
        TEXT,
        CheckConstraint("reason IN ('unsubscribe', 'complaint', 'bounce_hard', 'manual', 'erasure_request')"),
        nullable=False)
    added_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    source: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    scope: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("'all'"))


class DispatchedJob(Base):
    __tablename__ = "dispatched_job"
    __table_args__ = (
        Index("idx_dispatched_job_campaign", "campaign_id"),
        Index("idx_dispatched_job_status", "status"),
    )

    job_id: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    campaign_id: Mapped[int] = mapped_column(INTEGER, ForeignKey("campaigns.id"), nullable=False)
    recipient_id: Mapped[int] = mapped_column(INTEGER, ForeignKey("contacts.id"), nullable=False)
    recipient_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    status: Mapped[str] = mapped_column(
        TEXT,
        CheckConstraint("status IN ('dispatched', 'acked', 'completed', 'failed', 'cancelled')"),
        nullable=False, server_default=text("'dispatched'"))
    dispatched_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    acked_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    content_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    resend_message_id: Mapped[str | None] = mapped_column(TEXT, nullable=True)

    campaign: Mapped[Campaign] = relationship(
        back_populates="dispatched_job", passive_deletes=True)
    contact: Mapped[Contact] = relationship(
        back_populates="dispatched_job", passive_deletes=True)


class DriftReport(Base):
    __tablename__ = "drift_report"
    __table_args__ = (
        Index("idx_drift_report_severity", "severity", "acknowledged_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    detected_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    category: Mapped[str] = mapped_column(
        TEXT,
        CheckConstraint("category IN ('missing_on_edge', 'missing_on_vault', 'status_mismatch', 'content_mismatch')"),
        nullable=False)
    severity: Mapped[str] = mapped_column(
        TEXT, CheckConstraint("severity IN ('INFO', 'WARN', 'CRITICAL')"), nullable=False)
    job_id: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    details_json: Mapped[str] = mapped_column(TEXT, nullable=False)
    acknowledged_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("idx_audit_occurred", "occurred_at"),
        Index("idx_audit_action", "action"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    occurred_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    actor: Mapped[str] = mapped_column(TEXT, nullable=False)
    action: Mapped[str] = mapped_column(TEXT, nullable=False)
    target: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    details_json: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class InboxRow(Base):
    __tablename__ = "inbox"
    __table_args__ = (
        Index("idx_inbox_row", "table_name", "row_id", "revision"),
        Index("idx_inbox_unmaterialised", "materialised_at"),
        Index("idx_inbox_unmaterialised_type", "table_name", "materialised_at"),
    )

    event_id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    table_name: Mapped[str] = mapped_column(TEXT, nullable=False)
    row_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    revision: Mapped[int] = mapped_column(INTEGER, nullable=False)
    actor: Mapped[str] = mapped_column(TEXT, nullable=False)
    modified_at: Mapped[str] = mapped_column(TEXT, nullable=False)
    key_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    encrypted_payload: Mapped[bytes | None] = mapped_column(BLOB, nullable=True)
    deleted: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    received_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    materialised_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class OutboxRow(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("idx_outbox_unpushed", "pushed_at", "outbox_id"),
        {"sqlite_autoincrement": True},
    )

    outbox_id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    table_name: Mapped[str] = mapped_column(TEXT, nullable=False)
    row_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    revision: Mapped[int] = mapped_column(INTEGER, nullable=False)
    actor: Mapped[str] = mapped_column(TEXT, nullable=False)
    modified_at: Mapped[str] = mapped_column(TEXT, nullable=False)
    key_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    encrypted_payload: Mapped[bytes | None] = mapped_column(BLOB, nullable=True)
    deleted: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    pushed_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    assigned_event_id: Mapped[int | None] = mapped_column(INTEGER, nullable=True)


class SyncState(Base):
    __tablename__ = "sync_state"

    key: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    value: Mapped[str] = mapped_column(TEXT, nullable=False)
    updated_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    value: Mapped[str] = mapped_column(TEXT, nullable=False)
    is_secret: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    updated_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    updated_by: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("'system'"))


class MetricsSnapshot(Base):
    __tablename__ = "metrics_snapshots"
    __table_args__ = (
        Index("idx_metrics_ts", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    ts: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    contacts: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    campaigns: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    dispatched: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    suppression: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    name: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)
    domain: Mapped[str] = mapped_column(TEXT, nullable=False)
    from_name: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("''"))
    from_email: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("''"))
    resend_key_enc: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    updated_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    footer_address: Mapped[str | None] = mapped_column(TEXT, nullable=True, server_default=text("NULL"))
    support_email: Mapped[str | None] = mapped_column(TEXT, nullable=True, server_default=text("NULL"))
    public_host: Mapped[str | None] = mapped_column(TEXT, nullable=True, server_default=text("NULL"))

    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="entity", passive_deletes=True)


class Operator(Base):
    __tablename__ = "operators"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    email: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    role: Mapped[str] = mapped_column(
        TEXT, CheckConstraint("role IN ('admin', 'operator')"), nullable=False, server_default=text("'operator'"))
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    last_login_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    active: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("1"))


class CampaignStage(Base):
    __tablename__ = "campaign_stages"
    __table_args__ = (
        UniqueConstraint("campaign_id", "stage_number"),
        Index("idx_campaign_stages_sched", "status", "scheduled_for"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    campaign_id: Mapped[int] = mapped_column(
        INTEGER, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    stage_number: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("1"))
    template_id: Mapped[int] = mapped_column(INTEGER, ForeignKey("templates.id"), nullable=False)
    scheduled_for: Mapped[str] = mapped_column(TEXT, nullable=False)
    status: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("'pending'"))
    dispatched_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    note: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))

    campaign: Mapped[Campaign] = relationship(
        back_populates="campaign_stages", passive_deletes=True)
    template: Mapped[Template] = relationship(
        back_populates="campaign_stages", passive_deletes=True)


class CampaignBatch(Base):
    __tablename__ = "campaign_batches"
    __table_args__ = (
        UniqueConstraint("campaign_id", "batch_no"),
        Index("idx_campaign_batches_campaign", "campaign_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    campaign_id: Mapped[int] = mapped_column(INTEGER, nullable=False)
    batch_no: Mapped[int] = mapped_column(INTEGER, nullable=False)
    size: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        TEXT,
        CheckConstraint("status IN ('pending','scheduled','sending','drained','cooldown','approved','done','cancelled')"),
        nullable=False, server_default=text("'pending'"))
    window_start: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    window_end: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    loaded_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    drained_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    approve_unlock_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    sent: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    delivered: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    opened: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    clicked: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    bounced: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    complained: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    unsub: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    dispatch_started_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class SeedAddress(Base):
    __tablename__ = "seed_addresses"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    email: Mapped[str] = mapped_column(TEXT, nullable=False, unique=True)
    provider: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    active: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))


class SeedPlacement(Base):
    __tablename__ = "seed_placements"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    batch_id: Mapped[int] = mapped_column(INTEGER, nullable=False)
    seed_email: Mapped[str] = mapped_column(TEXT, nullable=False)
    placement: Mapped[str | None] = mapped_column(
        TEXT, CheckConstraint("placement IN ('inbox','spam','promotions','missing','unknown')"), nullable=True)
    checked_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    note: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class Migration(Base):
    __tablename__ = "_migration"

    name: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    applied_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
