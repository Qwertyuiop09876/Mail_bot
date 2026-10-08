"""Campaign lifecycle: create → validate → schedule → pause/resume/cancel → stats.

State machine::

    draft ──schedule──▶ scheduled ──(dispatcher, at scheduled_at)──▶ sending ──▶ completed
      ▲                    │  ▲                                        │  ▲
      └────unschedule──────┘  └─────────────── resume ─────────────────┤  │
                              paused ◀──────────── pause / account error┘  │
                                 └───────────────────────────────────────────┘
    scheduled | sending | paused ──cancel──▶ cancelled
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .db import Database
from .errors import NotFoundError, StateError, ValidationError
from .models import (
    Account,
    Campaign,
    CampaignStatus,
    Contact,
    ContactStatus,
    Delivery,
    DeliveryStatus,
    MailingList,
    Template,
    list_members,
    utcnow,
)
from .rendering import Rendered, build_context, check_syntax, render
from .unsubscribe import UnsubscribeSigner

_PREVIEW_UNSUBSCRIBE_URL = "https://example.com/unsubscribe/preview"
_SAMPLE_CONTACT = {"first_name": "Иван", "last_name": "Петров", "email": "ivan@example.com"}


@dataclass
class ValidationReport:
    recipients: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class CampaignStats:
    campaign_id: int
    status: CampaignStatus
    total: int
    pending: int
    sending: int
    sent: int
    failed: int
    skipped: int

    @property
    def remaining(self) -> int:
        return self.pending + self.sending


def recipient_ids_stmt(list_ids: Sequence[int]) -> Any:
    """IDs of distinct *active* contacts across the given lists (suppressed ones are excluded)."""
    return (
        select(Contact.id)
        .join(list_members, list_members.c.contact_id == Contact.id)
        .where(list_members.c.list_id.in_(list_ids), Contact.status == ContactStatus.ACTIVE)
        .distinct()
    )


class CampaignService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        signer: UnsubscribeSigner | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._signer = signer
        self._clock = clock

    # ---- creation --------------------------------------------------------------------------

    def create(
        self,
        name: str,
        *,
        account: str,
        lists: Sequence[str],
        template: str | None = None,
        subject: str | None = None,
        html: str | None = None,
        text: str | None = None,
    ) -> Campaign:
        """Create a draft. Content comes from ``template`` and/or explicit ``subject``/``html``/
        ``text``
        (explicit values win). The content is copied, so later template edits don't affect it."""
        if not name.strip():
            raise ValidationError("Campaign name must not be empty")
        if not lists:
            raise ValidationError("At least one list is required")
        with self._db.session() as s:
            acc = s.scalar(select(Account).where(Account.name == account))
            if acc is None:
                raise NotFoundError(f"Account {account!r} not found")
            list_objs = self._lists(s, lists)
            tpl: Template | None = None
            if template is not None:
                tpl = s.scalar(select(Template).where(Template.name == template))
                if tpl is None:
                    raise NotFoundError(f"Template {template!r} not found")
            subj = subject if subject is not None else (tpl.subject if tpl else None)
            body = html if html is not None else (tpl.html if tpl else None)
            plain = text if text is not None else (tpl.text if tpl else None)
            if not subj or not subj.strip():
                raise ValidationError("Subject is required (pass subject= or a template)")
            if not body or not body.strip():
                raise ValidationError("HTML body is required (pass html= or a template)")
            check_syntax(subject=subj, html=body, text=plain)
            campaign = Campaign(
                name=name.strip(),
                account_id=acc.id,
                template_id=tpl.id if tpl else None,
                subject=subj,
                html=body,
                text=plain,
                lists=list_objs,
            )
            s.add(campaign)
            s.flush()
            return campaign

    def update_content(
        self,
        campaign_id: int,
        *,
        subject: str | None = None,
        html: str | None = None,
        text: str | None = None,
    ) -> Campaign:
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(campaign, CampaignStatus.DRAFT, action="edit content")
            new_subject = subject if subject is not None else campaign.subject
            new_html = html if html is not None else campaign.html
            new_text = text if text is not None else campaign.text
            check_syntax(subject=new_subject, html=new_html, text=new_text)
            campaign.subject, campaign.html, campaign.text = new_subject, new_html, new_text
            return campaign

    def get(self, campaign_id: int) -> Campaign:
        with self._db.session() as s:
            return self._campaign(s, campaign_id)

    def all(self, *, status: CampaignStatus | None = None) -> list[Campaign]:
        with self._db.session() as s:
            stmt = select(Campaign).order_by(Campaign.id.desc())
            if status is not None:
                stmt = stmt.where(Campaign.status == status)
            return list(s.scalars(stmt))

    def delete(self, campaign_id: int) -> None:
        """Only drafts can be deleted; sent campaigns are history."""
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(campaign, CampaignStatus.DRAFT, action="delete")
            s.delete(campaign)

    # ---- preview / validation --------------------------------------------------------------

    def preview(self, campaign_id: int, *, email: str | None = None) -> Rendered:
        """Render the campaign for one contact (default: the first recipient, else a sample)."""
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            contact: Contact | None = None
            if email is not None:
                from .emails import normalize_email

                contact = s.scalar(select(Contact).where(Contact.email == normalize_email(email)))
                if contact is None:
                    raise NotFoundError(f"Contact {email!r} not found")
            else:
                ids = [lst.id for lst in campaign.lists]
                first = s.scalar(recipient_ids_stmt(ids).order_by(Contact.id).limit(1))
                if first is not None:
                    contact = s.get(Contact, first)
            if contact is not None:
                context = build_context(
                    email=contact.email,
                    first_name=contact.first_name,
                    last_name=contact.last_name,
                    attributes=contact.attributes,
                    unsubscribe_url=_PREVIEW_UNSUBSCRIBE_URL,
                )
            else:
                context = build_context(
                    attributes={}, unsubscribe_url=_PREVIEW_UNSUBSCRIBE_URL, **_SAMPLE_CONTACT
                )
            return render(
                subject=campaign.subject, html=campaign.html, text=campaign.text, context=context
            )

    def validate(self, campaign_id: int) -> ValidationReport:
        """Everything that can be checked before sending, including a dry-run render for every
        recipient, so a missing variable is found now instead of mid-mailing."""
        report = ValidationReport()
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            account = s.get(Account, campaign.account_id)
            assert account is not None
            if not self._settings.unsubscribe_base_url or self._signer is None:
                report.errors.append(
                    "MAILBOT_UNSUBSCRIBE_BASE_URL is not set: bulk mail needs a working "
                    "unsubscribe link"
                )
            ids = list(s.scalars(recipient_ids_stmt([lst.id for lst in campaign.lists])))
            report.recipients = len(ids)
            if not ids:
                report.errors.append("No active recipients in the selected lists")

            failures: list[str] = []
            for start in range(0, len(ids), 500):
                chunk = s.scalars(select(Contact).where(Contact.id.in_(ids[start : start + 500])))
                for contact in chunk:
                    try:
                        render(
                            subject=campaign.subject,
                            html=campaign.html,
                            text=campaign.text,
                            context=build_context(
                                email=contact.email,
                                first_name=contact.first_name,
                                last_name=contact.last_name,
                                attributes=contact.attributes,
                                unsubscribe_url=_PREVIEW_UNSUBSCRIBE_URL,
                            ),
                        )
                    except ValidationError as exc:
                        failures.append(f"{contact.email}: {exc}")
            if failures:
                shown = "; ".join(failures[:3])
                report.errors.append(
                    f"{len(failures)} recipient(s) cannot be rendered, e.g. {shown}. "
                    "Fix the data or use {{ var | default('...') }} in the template."
                )

            if "unsubscribe_url" not in campaign.html and "unsubscribe_url" not in (
                campaign.text or ""
            ):
                report.warnings.append(
                    "The body has no {{ unsubscribe_url }} link. The List-Unsubscribe header is "
                    "still added, but a visible link in the footer is strongly recommended."
                )
            if not campaign.text:
                report.warnings.append(
                    "No plain-text part: it will be generated from the HTML automatically."
                )
            if report.recipients > account.daily_limit:
                days = -(-report.recipients // account.daily_limit)
                report.warnings.append(
                    f"{report.recipients} recipients exceed the account's daily limit "
                    f"({account.daily_limit}); sending will take about {days} days."
                )
        return report

    # ---- lifecycle -------------------------------------------------------------------------

    def schedule(self, campaign_id: int, at: datetime | None = None) -> Campaign:
        """Queue the campaign for ``at`` (naive values are read in MAILBOT_TIMEZONE; ``None`` means
        as soon as the worker picks it up). Raises ValidationError if validation fails."""
        when = self._to_utc(at) if at is not None else self._clock()
        report = self.validate(campaign_id)
        if not report.ok:
            raise ValidationError("Campaign is not ready: " + " | ".join(report.errors))
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(campaign, CampaignStatus.DRAFT, action="schedule")
            campaign.status = CampaignStatus.SCHEDULED
            campaign.scheduled_at = when
            campaign.pause_reason = None
            return campaign

    def send_now(self, campaign_id: int) -> Campaign:
        return self.schedule(campaign_id, None)

    def unschedule(self, campaign_id: int) -> Campaign:
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(campaign, CampaignStatus.SCHEDULED, action="unschedule")
            campaign.status = CampaignStatus.DRAFT
            campaign.scheduled_at = None
            return campaign

    def reschedule(self, campaign_id: int, at: datetime) -> Campaign:
        when = self._to_utc(at)
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(campaign, CampaignStatus.SCHEDULED, action="reschedule")
            campaign.scheduled_at = when
            return campaign

    def pause(self, campaign_id: int, reason: str = "paused manually") -> Campaign:
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(
                campaign, CampaignStatus.SCHEDULED, CampaignStatus.SENDING, action="pause"
            )
            campaign.status = CampaignStatus.PAUSED
            campaign.pause_reason = reason
            return campaign

    def resume(self, campaign_id: int) -> Campaign:
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(campaign, CampaignStatus.PAUSED, action="resume")
            # started_at is set once recipients were materialised: then we were mid-sending.
            campaign.status = (
                CampaignStatus.SENDING if campaign.started_at else CampaignStatus.SCHEDULED
            )
            campaign.pause_reason = None
            return campaign

    def cancel(self, campaign_id: int) -> Campaign:
        """Stop for good. Messages already sent stay sent; the rest are skipped."""
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(
                campaign,
                CampaignStatus.SCHEDULED,
                CampaignStatus.SENDING,
                CampaignStatus.PAUSED,
                action="cancel",
            )
            s.execute(
                update(Delivery)
                .where(
                    Delivery.campaign_id == campaign.id,
                    Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.SENDING]),
                )
                .values(
                    status=DeliveryStatus.SKIPPED,
                    last_error="campaign cancelled",
                    updated_at=self._clock(),
                )
            )
            campaign.status = CampaignStatus.CANCELLED
            campaign.finished_at = self._clock()
            return campaign

    def retry_failed(self, campaign_id: int) -> int:
        """Give failed deliveries another go (e.g. after fixing the cause). Returns how many.

        Includes deliveries marked as interrupted by a crash; only retry those if you have checked
        the recipient did not already get the message.
        """
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            self._require(
                campaign,
                CampaignStatus.SENDING,
                CampaignStatus.PAUSED,
                CampaignStatus.COMPLETED,
                action="retry failed deliveries",
            )
            result = s.execute(
                update(Delivery)
                .where(
                    Delivery.campaign_id == campaign.id, Delivery.status == DeliveryStatus.FAILED
                )
                .values(
                    status=DeliveryStatus.PENDING,
                    attempts=0,
                    next_attempt_at=self._clock(),
                    last_error=None,
                    updated_at=self._clock(),
                )
            )
            count = int(result.rowcount or 0)  # type: ignore[attr-defined]
            if count and campaign.status is CampaignStatus.COMPLETED:
                campaign.status = CampaignStatus.SENDING
                campaign.finished_at = None
            return count

    def stats(self, campaign_id: int) -> CampaignStats:
        with self._db.session() as s:
            campaign = self._campaign(s, campaign_id)
            rows = dict(
                s.execute(
                    select(Delivery.status, func.count())
                    .where(Delivery.campaign_id == campaign_id)
                    .group_by(Delivery.status)
                ).all()
            )
            counts = {st: rows.get(st, 0) for st in DeliveryStatus}
            return CampaignStats(
                campaign_id=campaign_id,
                status=campaign.status,
                total=sum(counts.values()),
                pending=counts[DeliveryStatus.PENDING],
                sending=counts[DeliveryStatus.SENDING],
                sent=counts[DeliveryStatus.SENT],
                failed=counts[DeliveryStatus.FAILED],
                skipped=counts[DeliveryStatus.SKIPPED],
            )

    # ---- internals -------------------------------------------------------------------------

    def _to_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._settings.tz)
        return value

    def _campaign(self, s: Session, campaign_id: int) -> Campaign:
        campaign = s.get(Campaign, campaign_id)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        return campaign

    @staticmethod
    def _require(campaign: Campaign, *allowed: CampaignStatus, action: str) -> None:
        if campaign.status not in allowed:
            wanted = " or ".join(a.value for a in allowed)
            raise StateError(
                f"Cannot {action}: campaign {campaign.id} is {campaign.status.value} "
                f"(must be {wanted})"
            )

    @staticmethod
    def _lists(s: Session, names: Sequence[str]) -> list[MailingList]:
        found: list[MailingList] = []
        for name in dict.fromkeys(names):
            lst = s.scalar(select(MailingList).where(MailingList.name == name))
            if lst is None:
                raise NotFoundError(f"List {name!r} not found")
            found.append(lst)
        return found
