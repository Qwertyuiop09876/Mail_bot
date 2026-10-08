from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import pytest

from mailbot import MailBot, Settings
from mailbot.crypto import generate_key
from mailbot.errors import DeliveryError
from mailbot.smtp import SmtpConfig

BASE_URL = "https://mail.example.com/unsubscribe"
START = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class FakeTransport:
    """Stands in for SMTP. ``behavior(message, recipient)`` may return an error to raise."""

    behavior: Callable[[EmailMessage, str], DeliveryError | None] = lambda _m, _r: None
    connect_error: DeliveryError | None = None
    sent: list[tuple[str, EmailMessage]] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    connects: int = 0
    closes: int = 0

    def connect(self) -> None:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error

    def send(self, message: EmailMessage, *, sender: str, recipient: str) -> None:
        self.attempts.append(recipient)
        error = self.behavior(message, recipient)
        if error is not None:
            raise error
        self.sent.append((recipient, message))

    def close(self) -> None:
        self.closes += 1

    @property
    def recipients(self) -> list[str]:
        return [r for r, _ in self.sent]


@dataclass
class Env:
    bot: MailBot
    transport: FakeTransport
    clock: FakeClock
    sleeps: list[float]

    def seed(
        self,
        n: int = 3,
        *,
        daily_limit: int = 500,
        rate_per_minute: int = 600,
        html: str = (
            '<p>Привет, {{ first_name }}!</p><a href="{{ unsubscribe_url }}">Отписаться</a>'
        ),
        subject: str = "Новости для {{ first_name }}",
    ) -> int:
        """Account + list of ``n`` contacts + draft campaign. Returns the campaign id."""
        bot = self.bot
        bot.accounts.add(
            "main",
            provider="custom",
            smtp_host="smtp.test",
            from_email="news@example.com",
            from_name="Новости",
            password="secret",
            daily_limit=daily_limit,
            rate_per_minute=rate_per_minute,
        )
        for i in range(n):
            bot.contacts.add(
                f"user{i}@test.org", first_name=f"Имя{i}", lists=["clients"], source="test"
            )
        return bot.campaigns.create(
            "Октябрь", account="main", lists=["clients"], subject=subject, html=html
        ).id

    def run_to_completion(self, max_ticks: int = 50) -> None:
        for _ in range(max_ticks):
            report = self.bot.dispatcher.tick()
            if report.idle and not report.completed:
                return


def _make_env(start: datetime = START, **overrides: Any) -> Env:
    params: dict[str, Any] = {
        "database_url": "sqlite:///:memory:",
        "secret_key": generate_key(),
        "unsubscribe_base_url": BASE_URL,
        "timezone": "Europe/Moscow",
        "retry_base_seconds": 60,
        "max_attempts": 3,
        **overrides,
    }
    settings = Settings(_env_file=None, **params)  # type: ignore[call-arg]
    transport = FakeTransport()
    clock = FakeClock(start)
    sleeps: list[float] = []

    def factory(_cfg: SmtpConfig) -> FakeTransport:
        return transport

    bot = MailBot(settings, transport_factory=factory, clock=clock, sleep=sleeps.append)
    bot.init_db()
    return Env(bot, transport, clock, sleeps)


@pytest.fixture
def env() -> Iterator[Env]:
    e = _make_env()
    yield e
    e.bot.close()


@pytest.fixture
def make_env() -> Iterator[Callable[..., Env]]:
    created: list[Env] = []

    def build(**overrides: Any) -> Env:
        e = _make_env(**overrides)
        created.append(e)
        return e

    yield build
    for e in created:
        e.bot.close()
