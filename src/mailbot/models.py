"""ORM models.

All timestamps are timezone-aware UTC. ``UTCDateTime`` enforces that at the DB boundary, because
SQLite would otherwise silently hand back naive datetimes.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as UTC and always returns aware UTC datetimes."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; attach a timezone first")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Security(enum.StrEnum):
    SSL = "ssl"  # implicit TLS, usually port 465
    STARTTLS = "starttls"  # upgrade on a plain connection, usually port 587
    NONE = "none"  # plaintext: only for local test servers


class ContactStatus(enum.StrEnum):
    ACTIVE = "active"
    UNSUBSCRIBED = "unsubscribed"
    BOUNCED = "bounced"
    COMPLAINED = "complained"


class CampaignStatus(enum.StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


def _enum(cls: type[enum.StrEnum]) -> Enum:
    return Enum(
        cls,
        native_enum=False,
        create_constraint=False,
        length=16,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


list_members = Table(
    "list_members",
    Base.metadata,
    Column("list_id", ForeignKey("mailing_lists.id", ondelete="CASCADE"), primary_key=True),
    Column("contact_id", ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
    Column("added_at", UTCDateTime, nullable=False, default=utcnow),
    Index("ix_list_members_contact_id", "contact_id"),
)

campaign_lists = Table(
    "campaign_lists",
    Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
    Column("list_id", ForeignKey("mailing_lists.id", ondelete="RESTRICT"), primary_key=True),
)


class Account(Base):
    """A sender mailbox with its SMTP (and optionally IMAP) credentials."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    provider: Mapped[str] = mapped_column(String(32))
    from_email: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(200))
    reply_to: Mapped[str | None] = mapped_column(String(320))

    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int]
    smtp_security: Mapped[Security] = mapped_column(_enum(Security))
    username: Mapped[str] = mapped_column(String(320))
    password_enc: Mapped[str] = mapped_column(Text)

    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int | None]

    daily_limit: Mapped[int]
    rate_per_minute: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def from_domain(self) -> str:
        return self.from_email.rsplit("@", 1)[-1]


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    first_name: Mapped[str | None] = mapped_column(String(200))
    last_name: Mapped[str | None] = mapped_column(String(200))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[ContactStatus] = mapped_column(
        _enum(ContactStatus), default=ContactStatus.ACTIVE
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status_campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MailingList(Base):
    __tablename__ = "mailing_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    subject: Mapped[str] = mapped_column(Text)
    html: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Campaign(Base):
    """One mailing. Content is copied from the template at creation so later template edits
    cannot change a campaign that is already scheduled."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id", ondelete="SET NULL"))

    subject: Mapped[str] = mapped_column(Text)
    html: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)

    status: Mapped[CampaignStatus] = mapped_column(
        _enum(CampaignStatus), default=CampaignStatus.DRAFT
    )
    pause_reason: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    lists: Mapped[list[MailingList]] = relationship(secondary=campaign_lists, lazy="selectin")

    __table_args__ = (Index("ix_campaigns_status_scheduled_at", "status", "scheduled_at"),)


class Delivery(Base):
    """One message to one recipient within a campaign."""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))  # snapshot: survives contact edits

    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus), default=DeliveryStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    smtp_code: Mapped[int | None]
    message_id: Mapped[str | None] = mapped_column(String(998))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_deliveries_campaign_contact"),
        Index("ix_deliveries_campaign_status_next", "campaign_id", "status", "next_attempt_at"),
        Index("ix_deliveries_sent_at", "sent_at"),
        Index("ix_deliveries_message_id", "message_id"),
    )
