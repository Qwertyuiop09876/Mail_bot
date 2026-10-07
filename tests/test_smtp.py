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
        (554, "5.7.1 Message rejected under suspicion of SPAM", MessageRejected),
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


def test_unrelated_exceptions_are_not_swallowed() -> None:
    with pytest.raises(KeyError):
        translate_exception(KeyError("bug"))
