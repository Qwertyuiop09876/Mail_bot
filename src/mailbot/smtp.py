"""SMTP transport and mapping of server replies to delivery outcomes.

The dispatcher depends only on the :class:`Transport` protocol, so tests (and a future
API-based sender) can replace the real SMTP connection.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from .errors import (
    AccountError,
    DeliveryError,
    MessageRejected,
    RecipientRejected,
    TransientDeliveryError,
)
from .models import Security


class Transport(Protocol):
    def connect(self) -> None:
        """Open and authenticate now; raise a :class:`DeliveryError` subclass on failure."""

    def send(self, message: EmailMessage, *, sender: str, recipient: str) -> None:
        """Send or raise a :class:`DeliveryError` subclass."""

    def close(self) -> None: ...


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: Security
    username: str
    password: str
    timeout: float = 30.0


_ENHANCED = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")

# Enhanced status codes (RFC 3463) meaning "this recipient address is no good".
_RECIPIENT_CODES = {"5.1.1", "5.1.2", "5.1.3", "5.1.5", "5.1.6", "5.1.10", "5.2.1"}
# ... and ones meaning "the *sender* is the problem" (From not owned by the login, bad or
# unresolvable sender domain). Checked before the 4xx rule: Postfix says "450 4.1.8 Sender
# address rejected: Domain not found", which retrying never fixes.
_SENDER_CODES = {"5.1.7", "5.1.8", "4.1.7", "4.1.8"}
# Broad wording, trusted only when the server gave no enhanced status code at all.
_UNKNOWN_USER = re.compile(
    r"user unknown|unknown user|no such user|mailbox (?:unavailable|not found|does not exist)"
    r"|does not exist|invalid (?:recipient|mailbox|address)|recipient (?:rejected|not found)"
    r"|нет такого|не существует",
    re.IGNORECASE,
)
# Narrow wording, enough to override a 5.7.x policy code. Yandex answers an unknown recipient with
# "550 5.7.1 No such user!". The broad pattern above must not be used here: "PTR record for the
# sending IP does not exist" is also a 5.7.x reply, but it is OUR problem, not the recipient's.
_NO_SUCH_USER = re.compile(
    r"no such user|user unknown|unknown user|mailbox (?:unavailable|not found)|нет такого",
    re.IGNORECASE,
)
# Yandex replies meaning "this *mailbox* is flagged as a spam source". Yandex's help says sending
# is then blocked for 24 hours and lifts only if no sending is attempted meanwhile, so the right
# reaction is to stop sending from the account at once (AccountError) and keep every recipient.
# Deliberately Yandex-specific phrases: generic ones ("looks like spam") also appear in per-message
# content filters of other providers, where pausing the whole mailbox would be wrong.
_SENDER_FLAGGED = re.compile(
    r"suspicion of spam|blocked by spam statistics|spamsource\.mail\.yandex\.net"
    r"|spam limit exceeded",
    re.IGNORECASE,
)
_FLAGGED_HINT = (
    " [this mailbox looks flagged as a spam source. Yandex's help says such a block lasts 24 hours"
    " and is lifted only if no messages are sent meanwhile, so retrying can prolong it: wait a day,"
    " check content/SPF/DKIM, then resume]"
)


def classify_smtp_error(code: int | None, text: str) -> DeliveryError:
    """Turn an SMTP reply into the error type that tells the dispatcher what to do."""
    msg = f"{code} {text}".strip() if code else text
    enhanced = _ENHANCED.search(text)
    enh = ".".join(enhanced.groups()) if enhanced else None
    sender_problem = "sender" in text.lower()

    if code in (530, 534, 535, 538) or enh in {"5.7.0", "5.7.8", "5.7.9", "4.7.8"}:
        return AccountError(msg, code=code)
    if enh in _SENDER_CODES:
        return AccountError(msg, code=code)
    if _SENDER_FLAGGED.search(text):  # before the 4xx rule: some variants are temporary-looking
        return AccountError(msg + _FLAGGED_HINT, code=code)

    if code is None or 400 <= code < 500:
        return TransientDeliveryError(msg, code=code)

    if enh in _RECIPIENT_CODES:
        return RecipientRejected(msg, code=code)
    # A permanent reply about the *sender* is our configuration (From not owned by the login,
    # sender domain unresolvable), never the recipient's fault: stop instead of bouncing everyone.
    if sender_problem and (enh is None or enh.startswith("5.7.")):
        return AccountError(msg, code=code)
    if code in (550, 551, 553):
        pattern = (
            _UNKNOWN_USER if enh is None else _NO_SUCH_USER if enh.startswith("5.7.") else None
        )
        if pattern is not None and pattern.search(text):
            return RecipientRejected(msg, code=code)
    if enh is None and code in (551, 553):
        return RecipientRejected(msg, code=code)
    return MessageRejected(msg, code=code)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def translate_exception(exc: Exception) -> DeliveryError:
    """Map any smtplib/network exception to a :class:`DeliveryError`."""
    if isinstance(exc, DeliveryError):
        return exc
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return AccountError(f"{exc.smtp_code} {_decode(exc.smtp_error)}", code=exc.smtp_code)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        # smtplib raises this for ANY non-250 reply to MAIL FROM, temporary ones included.
        reply = _decode(exc.smtp_error)
        classified = classify_smtp_error(exc.smtp_code, reply)
        if isinstance(classified, TransientDeliveryError | AccountError):
            return classified
        return AccountError(f"sender refused: {exc.smtp_code} {reply}", code=exc.smtp_code)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # One recipient per message, so there is exactly one entry.
        code, text = next(iter(exc.recipients.values()))
        return classify_smtp_error(code, _decode(text))
    if isinstance(exc, smtplib.SMTPResponseException):
        return classify_smtp_error(exc.smtp_code, _decode(exc.smtp_error))
    if isinstance(exc, UnicodeError):
        # smtplib sends login and password as ASCII; a Cyrillic keyboard layout or a malformed host
        # name ("a..b") ends up here. It is a setup mistake, not a server answer.
        return AccountError(f"invalid characters in the SMTP login, password or host: {exc}")
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return AccountError(f"server does not support a required feature: {exc}")
    if isinstance(exc, smtplib.SMTPException | OSError | ssl.SSLError):
        # Disconnects, timeouts, DNS failures, TLS hiccups: all worth a retry.
        return TransientDeliveryError(f"{type(exc).__name__}: {exc}")
    raise exc


class SmtpTransport:
    """Reuses one connection across messages and reconnects when the server drops it."""

    def __init__(self, config: SmtpConfig) -> None:
        self._cfg = config
        self._conn: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        cfg = self._cfg
        if cfg.security is Security.SSL:
            conn: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=cfg.timeout, context=ssl.create_default_context()
            )
        else:
            conn = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        try:
            if cfg.security is Security.STARTTLS:
                conn.starttls(context=ssl.create_default_context())
            conn.login(cfg.username, cfg.password)
        except BaseException:
            conn.close()  # don't leak the socket when TLS or login fails
            raise
        return conn

    def connect(self) -> None:
        """Open (and authenticate) the connection now; raises DeliveryError on failure."""
        if self._conn is not None:
            return
        try:
            self._conn = self._connect()
        except Exception as exc:
            raise translate_exception(exc) from exc

    def send(self, message: EmailMessage, *, sender: str, recipient: str) -> None:
        for attempt in (1, 2):
            self.connect()
            assert self._conn is not None
            try:
                self._conn.send_message(message, from_addr=sender, to_addrs=[recipient])
                return
            except smtplib.SMTPServerDisconnected as exc:
                # Idle connections get closed by servers; reconnect once before giving up.
                self.close()
                if attempt == 2:
                    raise translate_exception(exc) from exc
            except Exception as exc:
                raise translate_exception(exc) from exc

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.quit()
        except Exception:
            conn.close()
