"""Bounce processing: read delivery-status notifications (DSN) from the sender mailbox over IMAP.

Many bounces arrive *after* the SMTP session ended (the receiving server accepted the message,
then failed to deliver it). Without reading them, dead addresses stay on the list and wreck the
sender's reputation. A hard bounce suppresses the contact.

Security: a DSN is only trusted if its original ``Message-ID`` belongs to a delivery *we* made to
that very address. Otherwise anyone could mail the mailbox a forged "bounce" to suppress contacts.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from typing import Any

from sqlalchemy import select

from .accounts import AccountService
from .contacts import ContactService
from .db import Database
from .errors import ConfigError, RecipientRejected
from .models import Delivery
from .smtp import classify_smtp_error

log = logging.getLogger(__name__)

_CODE = re.compile(r"\b([45]\d\d)\b")


@dataclass(frozen=True)
class Bounce:
    recipient: str
    status: str  # RFC 3463 enhanced status, e.g. "5.1.1"
    diagnostic: str
    original_message_id: str | None

    @property
    def hard(self) -> bool:
        """True if the failure is about the recipient address itself (not content/quota/etc.)."""
        if not self.status.startswith("5."):
            return False
        code_match = _CODE.search(self.diagnostic)
        code = int(code_match.group(1)) if code_match else 550
        return isinstance(
            classify_smtp_error(code, f"{self.status} {self.diagnostic}"), RecipientRejected
        )


@dataclass
class BounceReport:
    scanned: int = 0
    hard: int = 0
    soft: int = 0
    unmatched: int = 0  # looked like a DSN but not tied to a delivery of ours: ignored
    suppressed: list[str] = field(default_factory=list)


def _header_block(part: Message) -> list[Message]:
    payload = part.get_payload()
    return [p for p in payload if isinstance(p, Message)] if isinstance(payload, list) else []


def parse_dsn(raw: bytes) -> list[Bounce]:
    """Extract failed recipients from a ``multipart/report`` DSN; empty list if it isn't one."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    if (
        msg.get_content_type() != "multipart/report"
        or msg.get_param("report-type") != "delivery-status"
    ):
        return []

    original_id: str | None = None
    status_blocks: list[Message] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            status_blocks = _header_block(part)
        elif ctype == "message/rfc822":
            inner = _header_block(part)
            if inner and inner[0]["Message-ID"]:
                original_id = str(inner[0]["Message-ID"]).strip()
        elif ctype == "text/rfc822-headers":
            headers = email.message_from_string(str(part.get_content()), policy=policy.default)
            if headers["Message-ID"]:
                original_id = str(headers["Message-ID"]).strip()

    bounces: list[Bounce] = []
    for block in status_blocks:  # first block describes the message; later ones the recipients
        recipient = block["Final-Recipient"] or block["Original-Recipient"]
        if not recipient or str(block["Action"]).strip().lower() != "failed":
            continue
        address = str(recipient).split(";", 1)[-1].strip().lower()
        status = str(block["Status"] or "").strip() or "5.0.0"
        diagnostic = str(block["Diagnostic-Code"] or "").split(";", 1)[-1].strip()
        bounces.append(Bounce(address, status, diagnostic, original_id))
    return bounces


class BounceScanner:
    def __init__(self, db: Database, contacts: ContactService, accounts: AccountService) -> None:
        self._db = db
        self._contacts = contacts
        self._accounts = accounts

    def scan(
        self,
        account_name: str,
        *,
        mailbox: str = "INBOX",
        limit: int = 500,
        imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
    ) -> BounceReport:
        """Process unread DSNs in the account's mailbox. Only processed DSNs are marked as read."""
        account = self._accounts.get(account_name)
        if not account.imap_host:
            raise ConfigError(f"Account {account_name!r} has no imap_host configured")
        cfg = self._accounts.smtp_config(account)  # same login/password for IMAP
        report = BounceReport()

        imap = imap_factory(account.imap_host, account.imap_port or 993, timeout=cfg.timeout)
        try:
            imap.login(cfg.username, cfg.password)
            imap.select(mailbox)
            _, data = imap.search(None, "UNSEEN")
            numbers = (data[0] or b"").split()[-limit:]
            for number in numbers:
                _, fetched = imap.fetch(number, "(BODY.PEEK[])")  # PEEK: don't mark as read yet
                raw = fetched[0][1] if fetched and isinstance(fetched[0], tuple) else None
                if not raw:
                    continue
                report.scanned += 1
                bounces = parse_dsn(raw)
                if not bounces:
                    continue
                for bounce in bounces:
                    self._apply(bounce, report)
                imap.store(number, "+FLAGS", "\\Seen")
        finally:
            try:
                imap.logout()
            except (imaplib.IMAP4.error, OSError):
                log.debug("IMAP logout failed", exc_info=True)
        return report

    def _apply(self, bounce: Bounce, report: BounceReport) -> None:
        campaign_id = self._matched_campaign(bounce)
        if campaign_id is None:
            report.unmatched += 1
            return
        if not bounce.hard:
            report.soft += 1
            return
        report.hard += 1
        reason = f"hard bounce {bounce.status}: {bounce.diagnostic}"[:1000]
        if self._contacts.mark_bounced(bounce.recipient, reason=reason, campaign_id=campaign_id):
            report.suppressed.append(bounce.recipient)

    def _matched_campaign(self, bounce: Bounce) -> int | None:
        if not bounce.original_message_id:
            return None
        with self._db.session() as s:
            row = s.execute(
                select(Delivery.campaign_id, Delivery.email).where(
                    Delivery.message_id == bounce.original_message_id
                )
            ).first()
        if row is None or row.email != bounce.recipient:
            return None
        return int(row.campaign_id)
