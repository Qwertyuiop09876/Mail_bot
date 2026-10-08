"""Sender accounts (Yandex or any custom-domain SMTP mailbox)."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select

from .config import Settings
from .crypto import SecretBox
from .db import Database
from .emails import normalize_email
from .errors import (
    DeliveryError,
    LoginFailedError,
    NotFoundError,
    StateError,
    ValidationError,
)
from .models import Account, Campaign, Security
from .providers import get_preset
from .smtp import SmtpConfig, SmtpTransport, Transport


def _login_is_sender(account: Account) -> bool:
    """Yandex wants From == login. A personal mailbox may log in with the bare name ("ivan"),
    which stands for ivan@yandex.ru."""
    login = account.username.lower()
    if "@" not in login:
        login = f"{login}@yandex.ru"
    return login == account.from_email.lower()


def _is_loopback(host: str) -> bool:
    """True only for the local machine. A string-prefix test would accept '127.0.0.1.evil.com'."""
    name = host.strip().rstrip(".").lower()
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _check_password(password: str) -> None:
    if not password:
        raise ValidationError("Password must not be empty")
    if not password.isascii():
        raise ValidationError(
            "The password contains non-ASCII characters; SMTP login (Python's smtplib) only "
            "supports ASCII. Check the keyboard layout: app passwords are plain Latin letters."
        )


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    detail: str
    warnings: list[str]


class AccountService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        box: SecretBox,
        transport_factory: Callable[[SmtpConfig], Transport] = SmtpTransport,
    ) -> None:
        self._db = db
        self._settings = settings
        self._box = box
        self._transport_factory = transport_factory

    def add(
        self,
        name: str,
        *,
        provider: str,
        from_email: str,
        password: str,
        from_name: str | None = None,
        reply_to: str | None = None,
        username: str | None = None,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_security: Security | str | None = None,
        imap_host: str | None = None,
        imap_port: int | None = None,
        daily_limit: int | None = None,
        rate_per_minute: int | None = None,
        verify: bool = False,
    ) -> Account:
        """Register a mailbox. Unspecified connection details come from the provider preset.

        For ``provider="custom"`` pass ``smtp_host`` (and ``smtp_port``/``smtp_security`` if they
        differ from 465/ssl). ``password`` is encrypted before it touches the database.

        With ``verify=True`` the SMTP login is tried *before* anything is saved and a failure raises
        :class:`LoginFailedError`, so no half-configured account is ever left behind.
        """
        preset = get_preset(provider)
        name = name.strip()
        if not name:
            raise ValidationError("Account name must not be empty")
        _check_password(password)
        from_email = normalize_email(from_email)
        host = smtp_host or preset.smtp_host
        if not host:
            raise ValidationError("smtp_host is required for a custom provider")
        security = Security(smtp_security) if smtp_security else preset.smtp_security
        if security is Security.NONE and not _is_loopback(host):
            raise ValidationError(
                "Unencrypted SMTP (smtp_security='none') would send the password in clear text; "
                "it is only allowed for localhost. Use 'ssl' or 'starttls'."
            )
        limit = daily_limit if daily_limit is not None else preset.daily_limit
        rate = rate_per_minute if rate_per_minute is not None else preset.rate_per_minute
        if limit < 1 or rate < 1:
            raise ValidationError("daily_limit and rate_per_minute must be positive")

        reply_to_norm = normalize_email(reply_to) if reply_to else None
        login = username or from_email
        port = smtp_port or preset.smtp_port
        with self._db.session() as s:
            if s.scalar(select(Account.id).where(Account.name == name)) is not None:
                raise ValidationError(f"Account {name!r} already exists")
        if verify:
            self._verify_login(
                SmtpConfig(
                    host=host,
                    port=port,
                    security=security,
                    username=login,
                    password=password,
                    timeout=self._settings.smtp_timeout_seconds,
                )
            )

        with self._db.session() as s:
            account = Account(
                name=name,
                provider=preset.key,
                from_email=from_email,
                from_name=from_name,
                reply_to=reply_to_norm,
                smtp_host=host,
                smtp_port=port,
                smtp_security=security,
                username=login,
                password_enc=self._box.encrypt(password),
                imap_host=imap_host or preset.imap_host,
                imap_port=imap_port or preset.imap_port,
                daily_limit=limit,
                rate_per_minute=rate,
            )
            s.add(account)
            s.flush()
            return account

    def get(self, name: str) -> Account:
        with self._db.session() as s:
            account = s.scalar(select(Account).where(Account.name == name))
            if account is None:
                raise NotFoundError(f"Account {name!r} not found")
            return account

    def all(self) -> list[Account]:
        with self._db.session() as s:
            return list(s.scalars(select(Account).order_by(Account.name)))

    def set_password(self, name: str, password: str, *, verify: bool = False) -> None:
        """Replace the stored password (e.g. after an app password was revoked).

        With ``verify=True`` the new password must log in first; otherwise nothing is changed.
        """
        _check_password(password)
        if verify:
            check = self.check_connection(name, password=password)
            if not check.ok:
                raise LoginFailedError(f"Login with the new password failed: {check.detail}")
        with self._db.session() as s:
            account = s.scalar(select(Account).where(Account.name == name))
            if account is None:
                raise NotFoundError(f"Account {name!r} not found")
            account.password_enc = self._box.encrypt(password)

    def set_limits(
        self, name: str, *, daily_limit: int | None = None, rate_per_minute: int | None = None
    ) -> Account:
        with self._db.session() as s:
            account = s.scalar(select(Account).where(Account.name == name))
            if account is None:
                raise NotFoundError(f"Account {name!r} not found")
            if daily_limit is not None:
                if daily_limit < 1:
                    raise ValidationError("daily_limit must be positive")
                account.daily_limit = daily_limit
            if rate_per_minute is not None:
                if rate_per_minute < 1:
                    raise ValidationError("rate_per_minute must be positive")
                account.rate_per_minute = rate_per_minute
            return account

    def delete(self, name: str) -> None:
        with self._db.session() as s:
            account = s.scalar(select(Account).where(Account.name == name))
            if account is None:
                raise NotFoundError(f"Account {name!r} not found")
            used = s.scalar(
                select(func.count()).select_from(Campaign).where(Campaign.account_id == account.id)
            )
            if used:
                raise StateError(f"Account {name!r} is used by {used} campaign(s)")
            s.delete(account)

    def smtp_config(self, account: Account, *, password: str | None = None) -> SmtpConfig:
        """Connection settings. ``password`` replaces the stored one without decrypting it, so a
        lost/changed MAILBOT_SECRET_KEY can still be recovered from by entering the password."""
        return SmtpConfig(
            host=account.smtp_host,
            port=account.smtp_port,
            security=account.smtp_security,
            username=account.username,
            password=password if password is not None else self._box.decrypt(account.password_enc),
            timeout=self._settings.smtp_timeout_seconds,
        )

    def sender_warnings(self, account: Account) -> list[str]:
        """Configuration smells that do not stop the login but will get messages rejected."""
        if account.provider in ("yandex", "yandex360") and not _login_is_sender(account):
            return [
                "Yandex requires the From address to match the login exactly; "
                "otherwise messages are rejected (553 'not owned by auth user')."
            ]
        return []

    def _verify_login(self, config: SmtpConfig) -> None:
        transport = self._transport_factory(config)
        try:
            transport.connect()
        except DeliveryError as exc:
            raise LoginFailedError(str(exc)) from exc
        finally:
            transport.close()

    def check_connection(self, name: str, *, password: str | None = None) -> ConnectionCheck:
        """Log in to the SMTP server without sending anything.

        ``password`` tries a candidate password instead of the stored one (nothing is saved).
        """
        account = self.get(name)
        warnings = self.sender_warnings(account)
        transport = self._transport_factory(self.smtp_config(account, password=password))
        try:
            transport.connect()
        except DeliveryError as exc:
            return ConnectionCheck(False, str(exc), warnings)
        finally:
            transport.close()
        return ConnectionCheck(
            True, f"Logged in to {account.smtp_host}:{account.smtp_port}", warnings
        )
