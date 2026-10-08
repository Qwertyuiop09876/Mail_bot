"""The sending engine: activates due campaigns and delivers messages within the account's limits.

Guarantees worth knowing:

* **At-most-once.** A delivery is marked ``sending`` and committed *before* the SMTP call. If the
  process dies in between, the row is later marked ``failed`` ("state unknown") instead of being
  re-sent, because a duplicate marketing e-mail is worse than a rare missing one.
  ``CampaignService.retry_failed`` re-queues them if you decide otherwise.
* **Rate and daily limits** are per account and shared by all its campaigns. The daily limit is a
  rolling 24 hours, which is the safe interpretation of provider "per day" limits.
* **Suppression is re-checked at send time**, so an unsubscribe between scheduling and sending is
  honoured.
* **Circuit breakers.** Bad credentials / sender rejected pause the account's campaigns; repeated
  content rejections pause that campaign; repeated temporary errors back the account off for a
  while.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, select, update

from .accounts import AccountService
from .campaigns import recipient_ids_stmt
from .config import Settings
from .db import Database
from .errors import (
    AccountError,
    DeliveryError,
    MessageRejected,
    RecipientRejected,
    TransientDeliveryError,
    ValidationError,
)
from .message import build_message
from .models import (
    Account,
    Campaign,
    CampaignStatus,
    Contact,
    ContactStatus,
    Delivery,
    DeliveryStatus,
    utcnow,
)
from .rendering import build_context, render
from .smtp import SmtpConfig, SmtpTransport, Transport
from .unsubscribe import UnsubscribeSigner

log = logging.getLogger(__name__)

MAX_PER_TICK = 50  # keep ticks short so pause/cancel and shutdown take effect promptly
_INSERT_CHUNK = 1000
_TEST_UNSUBSCRIBE_URL = "https://example.com/unsubscribe/test"  # placeholder, never a real token
_DAY = timedelta(hours=24)


@dataclass
class TickReport:
    activated: list[int] = field(default_factory=list)
    sent: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0
    completed: list[int] = field(default_factory=list)
    paused: list[tuple[int, str]] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        return not (self.activated or self.sent or self.retried or self.failed or self.skipped)


@dataclass(frozen=True)
class _Job:
    delivery_id: int
    campaign_id: int
    contact_id: int
    email: str
    first_name: str | None
    last_name: str | None
    attributes: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class _CampaignContent:
    subject: str
    html: str
    text: str | None


class Dispatcher:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        accounts: AccountService,
        signer: UnsubscribeSigner | None,
        *,
        transport_factory: Callable[[SmtpConfig], Transport] = SmtpTransport,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._settings = settings
        self._accounts = accounts
        self._signer = signer
        self._transport_factory = transport_factory
        self._clock = clock
        self._stop = threading.Event()
        self._sleep = sleep or (lambda seconds: self._stop.wait(seconds))
        self._monotonic = monotonic
        self._backoff_until: dict[int, datetime] = {}

    # ---- public ----------------------------------------------------------------------------

    def tick(self) -> TickReport:
        """One pass: activate due campaigns, send what the limits allow, close finished ones."""
        report = TickReport()
        now = self._clock()
        self.recover_interrupted(now)
        self._activate_due(now, report)
        for account_id in self._accounts_with_work():
            blocked_until = self._backoff_until.get(account_id)
            if blocked_until is not None and blocked_until > now:
                continue
            try:
                self._send_for_account(account_id, report)
            except Exception:
                # One account's unexpected failure must not starve the accounts after it.
                log.exception(
                    "sending for account %d failed; continuing with the others", account_id
                )
            if self._stop.is_set():
                break
        self._complete_finished(report)
        return report

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Worker loop. Set ``stop`` (e.g. from a signal handler) for a graceful shutdown."""
        if stop is not None:
            self._stop = stop
        log.info("worker started (poll interval %.1fs)", self._settings.poll_interval_seconds)
        while not self._stop.is_set():
            try:
                report = self.tick()
                if not report.idle:
                    log.info(
                        "tick: activated=%s sent=%d retry=%d failed=%d skipped=%d completed=%s",
                        report.activated,
                        report.sent,
                        report.retried,
                        report.failed,
                        report.skipped,
                        report.completed,
                    )
            except Exception:
                log.exception("tick failed; will retry")
            self._stop.wait(self._settings.poll_interval_seconds)
        log.info("worker stopped")

    def request_stop(self) -> None:
        self._stop.set()

    def recover_interrupted(self, now: datetime | None = None) -> int:
        """Mark deliveries stuck in ``sending`` (crash mid-send) as failed with unknown state."""
        now = now or self._clock()
        cutoff = now - timedelta(seconds=self._settings.stale_sending_seconds)
        with self._db.session() as s:
            result = s.execute(
                update(Delivery)
                .where(Delivery.status == DeliveryStatus.SENDING, Delivery.updated_at < cutoff)
                .values(
                    status=DeliveryStatus.FAILED,
                    last_error=(
                        "interrupted while sending; delivery state unknown, not retried "
                        "automatically to avoid duplicates"
                    ),
                    updated_at=now,
                )
            )
            count = int(result.rowcount or 0)  # type: ignore[attr-defined]
        if count:
            log.warning("%d delivery(ies) were interrupted by a crash and marked failed", count)
        return count

    def send_test(self, campaign_id: int, to_email: str) -> str:
        """Send one real copy of the campaign to ``to_email`` (subject prefixed ``[TEST]``).

        Not recorded as a delivery and never touches the contact list. Raises DeliveryError if the
        server refuses. Returns the Message-ID.
        """
        from .emails import normalize_email
        from .errors import NotFoundError

        recipient = normalize_email(to_email)
        with self._db.session() as s:
            campaign = s.get(Campaign, campaign_id)
            if campaign is None:
                raise NotFoundError(f"Campaign {campaign_id} not found")
            account = s.get(Account, campaign.account_id)
            assert account is not None
            contact = s.scalar(select(Contact).where(Contact.email == recipient))
            context = build_context(
                email=recipient,
                first_name=contact.first_name if contact else "Тест",
                last_name=contact.last_name if contact else None,
                attributes=dict(contact.attributes) if contact else {},
                unsubscribe_url=_TEST_UNSUBSCRIBE_URL,
            )
            rendered = render(
                subject=campaign.subject, html=campaign.html, text=campaign.text, context=context
            )
        message = build_message(
            from_email=account.from_email,
            from_name=account.from_name,
            to_email=recipient,
            reply_to=account.reply_to,
            subject=f"[TEST] {rendered.subject}",
            html=rendered.html,
            text=rendered.text,
            unsubscribe_url=_TEST_UNSUBSCRIBE_URL,  # same headers as a real send; the link is inert
            date=self._clock(),
        )
        transport = self._transport_factory(self._accounts.smtp_config(account))
        try:
            transport.connect()
            transport.send(message, sender=account.from_email, recipient=recipient)
        finally:
            transport.close()
        return str(message["Message-ID"])

    # ---- activation ------------------------------------------------------------------------

    def _activate_due(self, now: datetime, report: TickReport) -> None:
        with self._db.session() as s:
            due = list(
                s.scalars(
                    select(Campaign.id).where(
                        Campaign.status == CampaignStatus.SCHEDULED, Campaign.scheduled_at <= now
                    )
                )
            )
        for campaign_id in due:
            with self._db.session() as s:
                campaign = s.get(Campaign, campaign_id)
                if campaign is None or campaign.status is not CampaignStatus.SCHEDULED:
                    continue
                if self._signer is None:
                    reason = "MAILBOT_UNSUBSCRIBE_BASE_URL is not configured"
                    campaign.status = CampaignStatus.PAUSED
                    campaign.pause_reason = reason
                    report.paused.append((campaign_id, reason))
                    log.error("campaign %d paused: unsubscribe URL is not configured", campaign_id)
                    continue
                ids = list(s.scalars(recipient_ids_stmt([lst.id for lst in campaign.lists])))
                for start in range(0, len(ids), _INSERT_CHUNK):
                    chunk = ids[start : start + _INSERT_CHUNK]
                    rows = s.execute(select(Contact.id, Contact.email).where(Contact.id.in_(chunk)))
                    s.execute(
                        insert(Delivery),
                        [
                            {
                                "campaign_id": campaign_id,
                                "contact_id": cid,
                                "email": email,
                                "status": DeliveryStatus.PENDING,
                                "attempts": 0,
                                "next_attempt_at": now,
                                "updated_at": now,
                            }
                            for cid, email in rows
                        ],
                    )
                campaign.status = CampaignStatus.SENDING
                campaign.started_at = now
                report.activated.append(campaign_id)
                log.info("campaign %d started with %d recipients", campaign_id, len(ids))

    def _complete_finished(self, report: TickReport) -> None:
        with self._db.session() as s:
            open_states = [DeliveryStatus.PENDING, DeliveryStatus.SENDING]
            has_open = (
                select(Delivery.id)
                .where(Delivery.campaign_id == Campaign.id, Delivery.status.in_(open_states))
                .exists()
            )
            for campaign in s.scalars(
                select(Campaign).where(Campaign.status == CampaignStatus.SENDING, ~has_open)
            ):
                campaign.status = CampaignStatus.COMPLETED
                campaign.finished_at = self._clock()
                report.completed.append(campaign.id)
                log.info("campaign %d completed", campaign.id)

    # ---- sending ---------------------------------------------------------------------------

    def _accounts_with_work(self) -> list[int]:
        with self._db.session() as s:
            return list(
                s.scalars(
                    select(Campaign.account_id)
                    .where(Campaign.status == CampaignStatus.SENDING)
                    .distinct()
                    .order_by(Campaign.account_id)
                )
            )

    def _sent_last_24h(self, account_id: int, now: datetime) -> int:
        with self._db.session() as s:
            return int(
                s.scalar(
                    select(func.count(Delivery.id))
                    .join(Campaign, Campaign.id == Delivery.campaign_id)
                    .where(
                        Campaign.account_id == account_id,
                        Delivery.status == DeliveryStatus.SENT,
                        Delivery.sent_at > now - _DAY,
                    )
                )
                or 0
            )

    def _send_for_account(self, account_id: int, report: TickReport) -> None:
        now = self._clock()
        with self._db.session() as s:
            account = s.get(Account, account_id)
            assert account is not None
        budget = account.daily_limit - self._sent_last_24h(account_id, now)
        if budget <= 0:
            log.info(
                "account %r reached its daily limit (%d); waiting",
                account.name,
                account.daily_limit,
            )
            return

        transport = self._transport_factory(self._accounts.smtp_config(account))
        try:
            # Connect up-front: a dead server or wrong password must not burn delivery attempts.
            transport.connect()
            self._send_loop(account, transport, budget, report)
        except AccountError as exc:
            self._pause_account(account_id, f"account error: {exc}", report)
        except TransientDeliveryError as exc:
            self._back_off(account_id, str(exc))
        except DeliveryError as exc:
            # A permanent, non-account reply to connect/EHLO/STARTTLS (e.g. our IP is blocklisted):
            # no recipient is involved, nothing will improve by retrying every few seconds.
            self._pause_account(account_id, f"server refused the connection: {exc}", report)
        finally:
            transport.close()

    def _send_loop(
        self, account: Account, transport: Transport, budget: int, report: TickReport
    ) -> None:
        min_interval = 60.0 / account.rate_per_minute
        content: dict[int, _CampaignContent] = {}
        rejected_in_row: dict[int, int] = {}
        transient_in_row = 0
        last_start: float | None = None

        for _ in range(MAX_PER_TICK):
            if budget <= 0 or self._stop.is_set():
                return
            job = self._claim_next(account.id)
            if job is None:
                return
            if last_start is not None:
                wait = min_interval - (self._monotonic() - last_start)
                if wait > 0:
                    self._sleep(wait)
            last_start = self._monotonic()

            outcome = self._deliver(job, account, transport, content)
            if outcome == "sent":
                report.sent += 1
                budget -= 1
                transient_in_row = 0
                rejected_in_row.pop(job.campaign_id, None)
            elif outcome == "skipped":
                report.skipped += 1
            elif outcome == "retry":
                report.retried += 1
                transient_in_row += 1
            elif outcome == "failed":
                report.failed += 1
            elif outcome == "rejected":
                report.failed += 1
                count = rejected_in_row.get(job.campaign_id, 0) + 1
                rejected_in_row[job.campaign_id] = count
                if count >= self._settings.circuit_breaker_threshold:
                    reason = f"{count} messages in a row were rejected by the server"
                    self._pause_campaign(job.campaign_id, reason, report)

            if transient_in_row >= self._settings.circuit_breaker_threshold:
                self._back_off(account.id, f"{transient_in_row} temporary errors in a row")
                return

    def _deliver(
        self,
        job: _Job,
        account: Account,
        transport: Transport,
        content_cache: dict[int, _CampaignContent],
    ) -> str:
        """Send one claimed delivery. Returns sent|skipped|retry|failed|rejected."""
        with self._db.session() as s:
            contact = s.get(Contact, job.contact_id)
            assert contact is not None
            status = contact.status
            if job.campaign_id not in content_cache:
                campaign = s.get(Campaign, job.campaign_id)
                assert campaign is not None
                content_cache[job.campaign_id] = _CampaignContent(
                    campaign.subject, campaign.html, campaign.text
                )
        if status is not ContactStatus.ACTIVE:
            self._finish(job, DeliveryStatus.SKIPPED, error=f"contact is {status.value}")
            return "skipped"
        content = content_cache[job.campaign_id]
        assert self._signer is not None
        unsubscribe_url = self._signer.make_url(job.contact_id, job.campaign_id)

        try:
            rendered = render(
                subject=content.subject,
                html=content.html,
                text=content.text,
                context=build_context(
                    email=job.email,
                    first_name=job.first_name,
                    last_name=job.last_name,
                    attributes=job.attributes,
                    unsubscribe_url=unsubscribe_url,
                ),
            )
        except ValidationError as exc:
            self._finish(job, DeliveryStatus.FAILED, error=f"render failed: {exc}")
            return "failed"

        message = build_message(
            from_email=account.from_email,
            from_name=account.from_name,
            to_email=job.email,
            to_name=" ".join(p for p in (job.first_name, job.last_name) if p) or None,
            reply_to=account.reply_to,
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
            unsubscribe_url=unsubscribe_url,
            date=self._clock(),
        )
        message_id = str(message["Message-ID"])
        try:
            transport.send(message, sender=account.from_email, recipient=job.email)
        except RecipientRejected as exc:
            self._finish(job, DeliveryStatus.FAILED, error=str(exc), code=exc.code)
            self._suppress_bounced(job, str(exc))
            return "failed"
        except MessageRejected as exc:
            self._finish(job, DeliveryStatus.FAILED, error=str(exc), code=exc.code)
            return "rejected"
        except TransientDeliveryError as exc:
            return self._retry_or_fail(job, exc)
        except AccountError as exc:
            # Put it back untouched; the whole account is paused by the caller's handler.
            self._finish(
                job, DeliveryStatus.PENDING, error=str(exc), code=exc.code, attempts=job.attempts
            )
            raise
        self._finish(job, DeliveryStatus.SENT, message_id=message_id, attempts=job.attempts + 1)
        return "sent"

    # ---- state transitions -----------------------------------------------------------------

    def _claim_next(self, account_id: int) -> _Job | None:
        now = self._clock()
        with self._db.session() as s:
            row = s.execute(
                select(Delivery, Contact)
                .join(Contact, Contact.id == Delivery.contact_id)
                .join(Campaign, Campaign.id == Delivery.campaign_id)
                .where(
                    Campaign.account_id == account_id,
                    Campaign.status == CampaignStatus.SENDING,
                    Delivery.status == DeliveryStatus.PENDING,
                    Delivery.next_attempt_at <= now,
                )
                .order_by(Delivery.campaign_id, Delivery.id)
                .limit(1)
            ).first()
            if row is None:
                return None
            delivery, contact = row
            delivery.status = DeliveryStatus.SENDING
            delivery.updated_at = now
            return _Job(
                delivery_id=delivery.id,
                campaign_id=delivery.campaign_id,
                contact_id=contact.id,
                email=delivery.email,
                first_name=contact.first_name,
                last_name=contact.last_name,
                attributes=dict(contact.attributes),
                attempts=delivery.attempts,
            )

    def _finish(
        self,
        job: _Job,
        status: DeliveryStatus,
        *,
        error: str | None = None,
        code: int | None = None,
        message_id: str | None = None,
        attempts: int | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        now = self._clock()
        with self._db.session() as s:
            delivery = s.get(Delivery, job.delivery_id)
            assert delivery is not None
            delivery.status = status
            delivery.last_error = error
            delivery.smtp_code = code
            if message_id:
                delivery.message_id = message_id
            if attempts is not None:
                delivery.attempts = attempts
            if next_attempt_at is not None:
                delivery.next_attempt_at = next_attempt_at
            if status is DeliveryStatus.SENT:
                delivery.sent_at = now
            delivery.updated_at = now

    def _retry_or_fail(self, job: _Job, exc: DeliveryError) -> str:
        attempts = job.attempts + 1
        if attempts >= self._settings.max_attempts:
            self._finish(
                job,
                DeliveryStatus.FAILED,
                error=f"gave up after {attempts} attempts: {exc}",
                code=exc.code,
                attempts=attempts,
            )
            return "failed"
        delay = self._settings.retry_base_seconds * 2 ** (attempts - 1)
        self._finish(
            job,
            DeliveryStatus.PENDING,
            error=str(exc),
            code=exc.code,
            attempts=attempts,
            next_attempt_at=self._clock() + timedelta(seconds=delay),
        )
        return "retry"

    def _suppress_bounced(self, job: _Job, reason: str) -> None:
        with self._db.session() as s:
            contact = s.get(Contact, job.contact_id)
            if contact is not None and contact.status is ContactStatus.ACTIVE:
                contact.status = ContactStatus.BOUNCED
                contact.status_reason = f"hard bounce: {reason}"[:1000]
                contact.status_changed_at = self._clock()
                contact.status_campaign_id = job.campaign_id

    def _pause_campaign(self, campaign_id: int, reason: str, report: TickReport) -> None:
        with self._db.session() as s:
            campaign = s.get(Campaign, campaign_id)
            if campaign is not None and campaign.status in (
                CampaignStatus.SENDING,
                CampaignStatus.SCHEDULED,
            ):
                campaign.status = CampaignStatus.PAUSED
                campaign.pause_reason = reason
                report.paused.append((campaign_id, reason))
                log.error("campaign %d paused: %s", campaign_id, reason)

    def _pause_account(self, account_id: int, reason: str, report: TickReport) -> None:
        with self._db.session() as s:
            ids = list(
                s.scalars(
                    # Scheduled campaigns too: otherwise one would activate later and hit the
                    # same block again, which for a spam block extends it.
                    select(Campaign.id).where(
                        Campaign.account_id == account_id,
                        Campaign.status.in_([CampaignStatus.SENDING, CampaignStatus.SCHEDULED]),
                    )
                )
            )
        for campaign_id in ids:
            self._pause_campaign(campaign_id, reason, report)

    def _back_off(self, account_id: int, reason: str) -> None:
        until = self._clock() + timedelta(seconds=self._settings.retry_base_seconds)
        self._backoff_until[account_id] = until
        log.warning("account %d backing off until %s: %s", account_id, until.isoformat(), reason)
