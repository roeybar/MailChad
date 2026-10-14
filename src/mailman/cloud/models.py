"""SQLAlchemy ORM models of the cloud SQLite schema.

One class per table of cloud/migrations/0001-0002 plus _migration. The
migrations stay the schema source: product code never calls create_all.
"""
from __future__ import annotations

from sqlalchemy import BLOB, INTEGER, REAL, TEXT, CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EventLog(Base):
    __tablename__ = "event_log"
    __table_args__ = (
        Index("idx_event_log_row", "table_name", "row_id", "revision"),
        Index("idx_event_log_actor", "actor", "received_at"),
        {"sqlite_autoincrement": True},
    )

    event_id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    table_name: Mapped[str] = mapped_column(TEXT, nullable=False)
    row_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    revision: Mapped[int] = mapped_column(INTEGER, nullable=False)
    actor: Mapped[str] = mapped_column(TEXT, CheckConstraint("actor IN ('operator', 'client')"), nullable=False)
    modified_at: Mapped[str] = mapped_column(TEXT, nullable=False)
    key_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    encrypted_payload: Mapped[bytes | None] = mapped_column(BLOB, nullable=True)
    deleted: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    received_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))

    webhook_event_raw: Mapped[list[WebhookEventRaw]] = relationship(
        back_populates="event_log", passive_deletes=True)


class Pack(Base):
    __tablename__ = "pack"
    __table_args__ = (
        Index("idx_pack_pending", "status", "send_at"),
        Index("idx_pack_campaign", "campaign_id"),
    )

    pack_id: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    campaign_id: Mapped[int] = mapped_column(INTEGER, nullable=False)
    recipient_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    content_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    send_at: Mapped[str] = mapped_column(TEXT, nullable=False)
    key_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    encrypted_payload: Mapped[bytes | None] = mapped_column(BLOB, nullable=True)
    status: Mapped[str] = mapped_column(
        TEXT,
        CheckConstraint("status IN ('pending', 'claimed', 'sent', 'failed', 'stuck_no_key', 'cancelled')"),
        nullable=False, server_default=text("'pending'"))
    enqueued_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    claimed_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    sent_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    resend_message_id: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    pushed_by: Mapped[str] = mapped_column(TEXT, CheckConstraint("pushed_by IN ('operator', 'client')"), nullable=False)


class NearConflictLog(Base):
    __tablename__ = "near_conflict_log"
    __table_args__ = (
        Index("idx_near_conflict_unack", "acknowledged_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    table_name: Mapped[str] = mapped_column(TEXT, nullable=False)
    row_id: Mapped[str] = mapped_column(TEXT, nullable=False)
    event_id_a: Mapped[int] = mapped_column(INTEGER, nullable=False)
    event_id_b: Mapped[int] = mapped_column(INTEGER, nullable=False)
    actor_a: Mapped[str] = mapped_column(TEXT, nullable=False)
    actor_b: Mapped[str] = mapped_column(TEXT, nullable=False)
    modified_at_a: Mapped[str] = mapped_column(TEXT, nullable=False)
    modified_at_b: Mapped[str] = mapped_column(TEXT, nullable=False)
    delta_seconds: Mapped[float] = mapped_column(REAL, nullable=False)
    detected_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    acknowledged_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class TerminalSession(Base):
    __tablename__ = "terminal_session"

    bearer_hash: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    actor: Mapped[str] = mapped_column(TEXT, CheckConstraint("actor IN ('operator', 'client')"), nullable=False)
    created_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    last_seen_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(TEXT, nullable=True)


class Pubkey(Base):
    __tablename__ = "pubkey"

    actor: Mapped[str | None] = mapped_column(
        TEXT, CheckConstraint("actor IN ('operator', 'client')"), primary_key=True, nullable=True)
    kem_pub: Mapped[bytes] = mapped_column(BLOB, nullable=False)
    registered_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))


class WebhookEventRaw(Base):
    __tablename__ = "webhook_event_raw"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int | None] = mapped_column(INTEGER, primary_key=True, nullable=True)
    svix_id: Mapped[str | None] = mapped_column(TEXT, nullable=True, unique=True)
    event_type: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    message_id: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    received_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    forwarded_event_id: Mapped[int | None] = mapped_column(
        INTEGER, ForeignKey("event_log.event_id"), nullable=True)

    event_log: Mapped[EventLog | None] = relationship(
        back_populates="webhook_event_raw", passive_deletes=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    value: Mapped[str] = mapped_column(TEXT, nullable=False)
    is_secret: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    updated_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
    updated_by: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("'system'"))


class Migration(Base):
    __tablename__ = "_migration"

    name: Mapped[str | None] = mapped_column(TEXT, primary_key=True, nullable=True)
    applied_at: Mapped[str] = mapped_column(TEXT, nullable=False, server_default=text("(datetime('now'))"))
