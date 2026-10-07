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
# ... and ones meaning "the *sender* is the problem" (From not owned by the login, bad syntax).
_SENDER_CODES = {"5.1.7", "5.1.8"}
_UNKNOWN_USER = re.compile(
    r"user unknown|unknown user|no such user|mailbox (?:unavailable|not found|does not exist)"
    r"|does not exist|invalid (?:recipient|mailbox|address)|recipient (?:rejected|not found)"
    r"|нет такого|не существует",
    re.IGNORECASE,
)


def classify_smtp_error(code: int | None, text: str) -> DeliveryError:
    """Turn an SMTP reply into the error type that tells the dispatcher what to do."""
    msg = f"{code} {text}".strip() if code else text
    enhanced = _ENHANCED.search(text)
    enh = ".".join(enhanced.groups()) if enhanced else None

    if code in (530, 534, 535, 538) or enh in {"5.7.0", "5.7.8", "5.7.9", "4.7.8"}:
        return AccountError(msg, code=code)
    if enh in _SENDER_CODES:
        return AccountError(msg, code=code)

    if code is None or 400 <= code < 500:
        return TransientDeliveryError(msg, code=code)

    if enh in _RECIPIENT_CODES:
        return RecipientRejected(msg, code=code)
    if enh and enh.startswith("5.7."):
        if "sender" in text.lower():
            return AccountError(msg, code=code)
        return MessageRejected(msg, code=code)
    if enh is None:
        if code in (551, 553):
            return RecipientRejected(msg, code=code)
        if code == 550 and _UNKNOWN_USER.search(text):
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
        return AccountError(
            f"sender refused: {exc.smtp_code} {_decode(exc.smtp_error)}", code=exc.smtp_code
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # One recipient per message, so there is exactly one entry.
        code, text = next(iter(exc.recipients.values()))
        return classify_smtp_error(code, _decode(text))
    if isinstance(exc, smtplib.SMTPResponseException):
        return classify_smtp_error(exc.smtp_code, _decode(exc.smtp_error))
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
