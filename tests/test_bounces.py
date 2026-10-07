from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest

from mailbot.bounces import Bounce, parse_dsn
from mailbot.errors import ConfigError
from mailbot.models import ContactStatus

from .conftest import Env


def make_dsn(
    recipient: str,
    original_id: str | None,
    *,
    status: str = "5.1.1",
    action: str = "failed",
    diagnostic: str = "smtp; 550 5.1.1 User unknown",
) -> bytes:
    """A realistic multipart/report DSN as produced by Postfix/Exim/Yandex."""
    msg = EmailMessage()
    msg["From"] = "MAILER-DAEMON@mx.example.org"
    msg["To"] = "news@example.com"
    msg["Subject"] = "Undelivered Mail Returned to Sender"
    msg.set_content("This is the mail system. Your message could not be delivered.")
    msg.make_mixed()  # placeholder; rebuilt below
    raw = (
        "From: MAILER-DAEMON@mx.example.org\r\n"
        "To: news@example.com\r\n"
        "Subject: Undelivered Mail Returned to Sender\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="BND"\r\n\r\n'
        "--BND\r\nContent-Type: text/plain\r\n\r\nCould not be delivered.\r\n"
        "--BND\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mx.example.org\r\n\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\n"
        f"Action: {action}\r\n"
        f"Status: {status}\r\n"
        f"Diagnostic-Code: {diagnostic}\r\n\r\n"
        "--BND\r\nContent-Type: text/rfc822-headers\r\n\r\n"
        + (f"Message-ID: {original_id}\r\n" if original_id else "")
        + "Subject: Новости\r\n\r\n--BND--\r\n"
    )
    return raw.encode()


class FakeImap:
    """Just enough of imaplib.IMAP4_SSL for the scanner."""

    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.seen: set[int] = set()
        self.logged_out = False

    def __call__(self, *_a: Any, **_kw: Any) -> FakeImap:
        return self

    def login(self, *_a: Any) -> None: ...
    def select(self, *_a: Any) -> None: ...

    def search(self, *_a: Any) -> tuple[str, list[bytes]]:
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, number: bytes, _what: str) -> tuple[str, list[Any]]:
        return "OK", [(b"1 (BODY[] {n}", self.messages[int(number) - 1]), b")"]

    def store(self, number: bytes, *_a: Any) -> None:
        self.seen.add(int(number))

    def logout(self) -> None:
        self.logged_out = True


def test_parse_dsn_extracts_recipient_status_and_original_id() -> None:
    (b,) = parse_dsn(make_dsn("Gone@Mx.org", "<abc@example.com>"))
    assert b == Bounce("gone@mx.org", "5.1.1", "550 5.1.1 User unknown", "<abc@example.com>")
    assert b.hard


def test_only_address_level_5xx_failures_are_hard() -> None:
    assert not Bounce("a@b.ru", "4.2.2", "452 mailbox full", None).hard
    assert not Bounce("a@b.ru", "5.7.1", "554 5.7.1 spam suspicion", None).hard
    assert not Bounce("a@b.ru", "5.2.2", "552 5.2.2 mailbox full", None).hard
    assert Bounce("a@b.ru", "5.1.1", "550 5.1.1 no such user", None).hard


def test_non_dsn_and_delayed_notices_are_ignored() -> None:
    assert parse_dsn(b"From: a@b.ru\r\nSubject: hi\r\n\r\nhello") == []
    assert parse_dsn(make_dsn("a@b.ru", "<x@y>", action="delayed", status="4.4.1")) == []


@pytest.fixture
def sent_env(env: Env) -> tuple[Env, str, str]:
    cid = env.seed(2)
    env.bot.campaigns.send_now(cid)
    env.run_to_completion()
    recipient, message = env.transport.sent[0]
    return env, recipient, str(message["Message-ID"])


def test_scan_suppresses_hard_bounce_for_our_own_delivery(sent_env) -> None:  # type: ignore[no-untyped-def]
    env, recipient, message_id = sent_env
    env.bot.accounts.set_limits("main")  # account exists
    with env.bot.db.session() as s:
        from mailbot.models import Account

        acc = s.query(Account).one()
        acc.imap_host = "imap.test"
    imap = FakeImap(
        [b"From: friend@x.ru\r\nSubject: lunch?\r\n\r\nhi", make_dsn(recipient, message_id)]
    )
    report = env.bot.bounces.scan("main", imap_factory=imap)

    assert (report.scanned, report.hard, report.suppressed) == (2, 1, [recipient])
    assert env.bot.contacts.get(recipient).status is ContactStatus.BOUNCED
    assert imap.seen == {2}  # the human's e-mail is left unread
    assert imap.logged_out


def test_forged_or_foreign_bounces_do_not_suppress_anyone(sent_env) -> None:  # type: ignore[no-untyped-def]
    env, recipient, message_id = sent_env
    with env.bot.db.session() as s:
        from mailbot.models import Account

        s.query(Account).one().imap_host = "imap.test"
    other = next(r for r, _ in env.transport.sent if r != recipient)
    imap = FakeImap(
        [
            make_dsn(recipient, "<not-ours@evil.example>"),  # unknown Message-ID
            make_dsn(recipient, None),  # no original id at all
            make_dsn(other, message_id),  # real id, but for a different address
        ]
    )
    report = env.bot.bounces.scan("main", imap_factory=imap)
    assert (report.hard, report.unmatched, report.suppressed) == (0, 3, [])
    assert env.bot.contacts.get(recipient).status is ContactStatus.ACTIVE
    assert env.bot.contacts.get(other).status is ContactStatus.ACTIVE


def test_scan_needs_imap_configured(env: Env) -> None:
    env.seed(1)
    with pytest.raises(ConfigError, match="imap_host"):
        env.bot.bounces.scan("main", imap_factory=FakeImap([]))
