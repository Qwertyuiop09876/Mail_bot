"""Building RFC-compliant MIME messages for bulk mail."""

from __future__ import annotations

from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid

from .models import utcnow


def build_message(
    *,
    from_email: str,
    from_name: str | None,
    to_email: str,
    to_name: str | None = None,
    reply_to: str | None = None,
    subject: str,
    html: str,
    text: str,
    unsubscribe_url: str | None,
    date: datetime | None = None,
) -> EmailMessage:
    """One message to one recipient.

    Deliverability-relevant details:
    * ``multipart/alternative`` with a plain-text part (HTML-only mail scores worse with filters);
    * single visible recipient, never BCC-style fan-out;
    * ``Message-ID`` on the sender's own domain;
    * ``List-Unsubscribe`` (+ RFC 8058 one-click when the URL is https) and ``Precedence: bulk``,
      which Gmail/Yahoo/Mail.ru require from bulk senders.
    """
    from_local, _, from_domain = from_email.rpartition("@")
    to_local, _, to_domain = to_email.rpartition("@")

    msg = EmailMessage(policy=SMTP)
    msg["From"] = Address(from_name or "", from_local, from_domain)
    msg["To"] = Address(to_name or "", to_local, to_domain)
    if reply_to:
        r_local, _, r_domain = reply_to.rpartition("@")
        msg["Reply-To"] = Address("", r_local, r_domain)
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date or utcnow())
    msg["Message-ID"] = make_msgid(domain=from_domain)
    msg["Precedence"] = "bulk"
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        if unsubscribe_url.startswith("https://"):
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg
