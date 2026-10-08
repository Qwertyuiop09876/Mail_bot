from __future__ import annotations

import smtplib

import pytest

from mailbot.errors import (
    AccountError,
    DeliveryError,
    MessageRejected,
    RecipientRejected,
    TransientDeliveryError,
)
from mailbot.smtp import classify_smtp_error, translate_exception


@pytest.mark.parametrize(
    ("code", "text", "expected"),
    [
        (550, "5.1.1 The email account that you tried to reach does not exist", RecipientRejected),
        (550, "5.1.1 <x@y.ru>: user unknown", RecipientRejected),
        (553, "5.1.3 invalid address", RecipientRejected),
        (550, "5.2.1 mailbox disabled", RecipientRejected),
        (550, "No such user here", RecipientRejected),
        (550, "Пользователь не существует", RecipientRejected),
        (551, "User not local", RecipientRejected),
        # Yandex-specific replies (see the research notes in CLAUDE.md / README)
        (550, "5.7.1 No such user!", RecipientRejected),  # unknown recipient with a policy code
        (550, "5.7.1 Policy rejection on the target address", MessageRejected),
        (552, "5.2.2 Mailbox size limit exceeded", MessageRejected),
        (554, "5.7.1 [1] Message rejected under suspicion of SPAM; https://ya.cc/x", AccountError),
        (554, "5.7.1 Blocked by spam statistics", AccountError),
        (554, "Client host [1.2.3.4] blocked using spamsource.mail.yandex.net", AccountError),
        (451, "4.7.1 Spam limit exceeded", AccountError),
        (535, "5.7.8 Error: authentication failed: Invalid user or password!", AccountError),
        (535, "5.7.8 Error: authentication failed: Please accept EULA first.", AccountError),
        (
            451,
            "4.7.1 Sorry, the service is currently unavailable. Please come back later.",
            TransientDeliveryError,
        ),
        (554, "5.7.1 Rejected by policy", MessageRejected),
        (550, "5.7.1 Rejected by policy", MessageRejected),
        (552, "5.2.2 mailbox full", MessageRejected),
        (554, "Transaction failed", MessageRejected),
        (535, "5.7.8 Error: authentication failed", AccountError),
        (530, "5.7.0 Must issue a STARTTLS command first", AccountError),
        (553, "5.7.1 Sender address rejected: not owned by auth user", AccountError),
        (550, "5.1.7 bad sender address syntax", AccountError),
        (451, "4.7.1 Greylisted, try later", TransientDeliveryError),
        (421, "Service not available", TransientDeliveryError),
        (452, "4.2.2 mailbox full, try later", TransientDeliveryError),
    ],
)
def test_classification(code: int, text: str, expected: type[DeliveryError]) -> None:
    err = classify_smtp_error(code, text)
    assert type(err) is expected
    assert err.code == code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (smtplib.SMTPAuthenticationError(535, b"5.7.8 bad"), AccountError),
        (smtplib.SMTPSenderRefused(550, b"5.7.1 sender not allowed", "a@b.ru"), AccountError),
        (
            smtplib.SMTPRecipientsRefused({"x@y.ru": (550, b"5.1.1 no such user")}),
            RecipientRejected,
        ),
        (smtplib.SMTPDataError(554, b"5.7.1 spam"), MessageRejected),
        (smtplib.SMTPServerDisconnected("gone"), TransientDeliveryError),
        (smtplib.SMTPConnectError(421, b"busy"), TransientDeliveryError),
        (TimeoutError("timed out"), TransientDeliveryError),
        (ConnectionResetError("reset"), TransientDeliveryError),
    ],
)
def test_translate_exception(exc: Exception, expected: type[DeliveryError]) -> None:
    assert type(translate_exception(exc)) is expected


def test_a_flagged_mailbox_keeps_its_recipient_and_explains_the_24h_rule() -> None:
    err = classify_smtp_error(554, "5.7.1 Message rejected under suspicion of SPAM")
    assert isinstance(err, AccountError) and "24 hours" in str(err) and "extends" in str(err)


def test_sender_wording_beats_the_unknown_user_text() -> None:
    # "does not exist" about the *sender* domain is our configuration problem, not a dead recipient
    err = classify_smtp_error(550, "5.7.1 Sender domain does not exist")
    assert isinstance(err, AccountError)


def test_unrelated_exceptions_are_not_swallowed() -> None:
    with pytest.raises(KeyError):
        translate_exception(KeyError("bug"))
