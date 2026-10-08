"""Real SMTP conversation against an in-process server (no mocks of smtplib)."""

from __future__ import annotations

import email
import socket
from collections.abc import Iterator
from email import policy
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from mailbot import MailBot, Settings
from mailbot.crypto import generate_key
from mailbot.errors import AccountError, MessageRejected, RecipientRejected
from mailbot.models import CampaignStatus, ContactStatus, Security
from mailbot.smtp import SmtpConfig, SmtpTransport

from .conftest import BASE_URL

# aiosmtpd (test server only) warns about plaintext AUTH and leaks a StreamWriter on closed sockets.
pytestmark = [
    pytest.mark.filterwarnings("ignore:Requiring AUTH while not requiring TLS"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


class Inbox:
    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.reject: dict[str, str] = {}
        self.data_reply: str | None = None  # e.g. a spam verdict returned after DATA

    async def handle_RCPT(self, _server, _session, envelope, address, _opts):  # type: ignore[no-untyped-def]
        if address in self.reject:
            return self.reject[address]
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, _server, _session, envelope):  # type: ignore[no-untyped-def]
        if self.data_reply is not None:
            return self.data_reply
        self.messages.append(envelope)
        return "250 Message accepted"


def authenticator(_server, _session, _envelope, mechanism, auth_data):  # type: ignore[no-untyped-def]
    ok = (
        isinstance(auth_data, LoginPassword)
        and auth_data.login == b"news@example.com"
        and auth_data.password == b"app-pass"
    )
    # handled=False makes aiosmtpd send the reply itself (its default, True, means "I already did").
    return AuthResult(success=ok, handled=False)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server() -> Iterator[tuple[Inbox, int]]:
    inbox, port = Inbox(), free_port()
    controller = Controller(
        inbox,
        hostname="127.0.0.1",
        port=port,
        authenticator=authenticator,
        auth_required=True,
        auth_require_tls=False,
    )
    controller.start()
    yield inbox, port
    controller.stop()


def cfg(port: int, password: str = "app-pass") -> SmtpConfig:  # noqa: S107
    return SmtpConfig("127.0.0.1", port, Security.NONE, "news@example.com", password, timeout=5)


def test_wrong_password_is_an_account_error(server) -> None:  # type: ignore[no-untyped-def]
    _, port = server
    with pytest.raises(AccountError):
        SmtpTransport(cfg(port, "wrong")).connect()


def test_unreachable_server_is_transient() -> None:
    from mailbot.errors import TransientDeliveryError

    with pytest.raises(TransientDeliveryError):
        SmtpTransport(cfg(free_port())).connect()


def test_full_campaign_through_a_real_smtp_server(server, tmp_path) -> None:  # type: ignore[no-untyped-def]
    inbox, port = server
    inbox.reject["gone@test.org"] = (
        "550 5.1.1 <gone@test.org>: Recipient address rejected: User unknown"
    )
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite:///{tmp_path}/real.db",
        secret_key=generate_key(),  # type: ignore[arg-type]
        unsubscribe_base_url=BASE_URL,
    )
    bot = MailBot(settings, sleep=lambda _s: None)
    bot.init_db()
    bot.accounts.add(
        "main",
        provider="custom",
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_security="none",
        from_email="news@example.com",
        from_name="Магазин «Весна»",
        password="app-pass",
        rate_per_minute=6000,
    )
    for name in ("anna", "oleg", "gone"):
        bot.contacts.add(f"{name}@test.org", first_name=name.title(), lists=["clients"])
    cid = bot.campaigns.create(
        "Осень",
        account="main",
        lists=["clients"],
        subject="Скидки для {{ first_name }}",
        html='<h1>Привет, {{ first_name }}!</h1><a href="{{ unsubscribe_url }}">Отписаться</a>',
    ).id
    bot.campaigns.send_now(cid)
    for _ in range(5):
        bot.dispatcher.tick()

    assert sorted(m.rcpt_tos[0] for m in inbox.messages) == ["anna@test.org", "oleg@test.org"]
    anna = next(m for m in inbox.messages if m.rcpt_tos == ["anna@test.org"])
    parsed = email.message_from_bytes(anna.content, policy=policy.default)
    assert parsed["Subject"] == "Скидки для Anna"
    assert parsed["From"].addresses[0].display_name == "Магазин «Весна»"
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "Привет, Anna!" in parsed.get_body(("html",)).get_content()  # type: ignore[union-attr]
    assert "Привет, Anna!" in parsed.get_body(("plain",)).get_content()  # type: ignore[union-attr]

    assert bot.contacts.get("gone@test.org").status is ContactStatus.BOUNCED
    stats = bot.campaigns.stats(cid)
    assert (stats.sent, stats.failed, stats.status) == (2, 1, CampaignStatus.COMPLETED)
    bot.close()


def test_server_rejecting_message_content_is_classified(server) -> None:  # type: ignore[no-untyped-def]
    inbox, port = server
    inbox.reject["spammy@test.org"] = "554 5.7.1 Rejected by policy"
    inbox.reject["nobody@test.org"] = "550 5.1.1 User unknown"
    from mailbot.message import build_message

    transport = SmtpTransport(cfg(port))
    try:
        msg = build_message(
            from_email="news@example.com",
            from_name=None,
            to_email="x@test.org",
            subject="s",
            html="<p>x</p>",
            text="x",
            unsubscribe_url=None,
        )
        with pytest.raises(MessageRejected):
            transport.send(msg, sender="news@example.com", recipient="spammy@test.org")
        with pytest.raises(RecipientRejected):
            transport.send(msg, sender="news@example.com", recipient="nobody@test.org")
        # The connection survives per-recipient rejections and keeps working.
        transport.send(msg, sender="news@example.com", recipient="fine@test.org")
    finally:
        transport.close()
    assert [m.rcpt_tos for m in inbox.messages] == [["fine@test.org"]]


def test_check_connection_reports_success_and_failure(server, tmp_path) -> None:  # type: ignore[no-untyped-def]
    _, port = server
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="sqlite:///:memory:",
        secret_key=generate_key(),  # type: ignore[arg-type]
    )
    with MailBot(settings) as bot:
        bot.init_db()
        common = {
            "provider": "custom",
            "smtp_host": "127.0.0.1",
            "smtp_port": port,
            "smtp_security": "none",
            "from_email": "news@example.com",
        }
        bot.accounts.add("good", password="app-pass", **common)
        bot.accounts.add("bad", password="nope", **common)
        assert bot.accounts.check_connection("good").ok
        bad = bot.accounts.check_connection("bad")
        assert not bad.ok and "535" in bad.detail
        ya = bot.accounts.add(
            "ya",
            provider="yandex",
            from_email="other@yandex.ru",
            username="login@yandex.ru",
            password="x",
            smtp_host="127.0.0.1",
            smtp_port=port,
            smtp_security="none",
        )
        assert ya.provider == "yandex"
        warnings = bot.accounts.check_connection("ya").warnings
        assert warnings and "match the login" in warnings[0]


# ---- `mailbot account-add` through a real SMTP server ------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAILBOT_SECRET_KEY", generate_key())
    monkeypatch.setenv("MAILBOT_DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")


def add_args(port: int, *extra: str) -> list[str]:
    return [
        "account-add", "main", "--provider", "custom", "--email", "news@example.com",
        "--smtp-host", "127.0.0.1", "--smtp-port", str(port), "--smtp-security", "none",
        "--from-name", "Магазин", *extra,
    ]  # fmt: skip


def test_account_add_saves_a_working_mailbox_and_hides_the_password(server, cli_env) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from mailbot.cli import cli

    _, port = server
    runner = CliRunner()
    result = runner.invoke(cli, add_args(port), input="app-pass\n")
    assert result.exit_code == 0, result.output
    assert "app-pass" not in result.output  # the prompt is hidden and nothing echoes it back
    assert "сохранён" in result.output

    listing = runner.invoke(cli, ["accounts"])
    assert "main [custom] news@example.com" in listing.output and "app-pass" not in listing.output

    # The stored copy is encrypted, not the plaintext.
    import sqlite3
    from pathlib import Path

    db_path = Path.cwd() / "cli.db"
    (stored,) = sqlite3.connect(db_path).execute("select password_enc from accounts").fetchone()
    assert "app-pass" not in stored


def test_account_add_rolls_back_when_login_fails(server, cli_env) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from mailbot.cli import cli

    _, port = server
    runner = CliRunner()
    result = runner.invoke(cli, add_args(port), input="wrong-password\n")
    assert result.exit_code == 1
    assert "ящик не сохранён" in result.output and "wrong-password" not in result.output
    assert "Ящиков нет" in runner.invoke(cli, ["accounts"]).output


def test_account_add_reads_the_password_from_the_environment(server, cli_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from mailbot.cli import cli

    _, port = server
    runner = CliRunner()
    monkeypatch.setenv("YA_PASS", "app-pass")
    assert runner.invoke(cli, add_args(port, "--password-env", "YA_PASS")).exit_code == 0

    runner = CliRunner()
    missing = runner.invoke(cli, add_args(port, "--password-env", "NOT_SET_ANYWHERE"))
    assert missing.exit_code != 0 and "NOT_SET_ANYWHERE" in missing.output


def test_account_add_no_check_works_offline_and_yandex_gets_its_preset(cli_env) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from mailbot.cli import cli

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "account-add",
            "ya",
            "--provider",
            "yandex360",
            "--email",
            "team@example.com",
            "--no-check",
        ],
        input="pw\n",
    )
    assert result.exit_code == 0 and "3000 писем/сутки" in result.output
    assert "smtp.yandex.ru:465/ssl" in runner.invoke(cli, ["accounts"]).output


def test_spam_block_stops_after_one_attempt_and_loses_nobody(server, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Yandex extends a spam block with every further attempt: stop at the first reply."""
    inbox, port = server
    inbox.data_reply = "554 5.7.1 [1] Message rejected under suspicion of SPAM; https://ya.cc/x"
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite:///{tmp_path}/spam.db",
        secret_key=generate_key(),  # type: ignore[arg-type]
        unsubscribe_base_url=BASE_URL,
    )
    attempts: list[str] = []
    real_factory = SmtpTransport

    class Counting(real_factory):  # type: ignore[valid-type, misc]
        def send(self, message, *, sender, recipient):  # type: ignore[no-untyped-def]
            attempts.append(recipient)
            super().send(message, sender=sender, recipient=recipient)

    with MailBot(settings, transport_factory=Counting, sleep=lambda _s: None) as bot:
        bot.init_db()
        bot.accounts.add(
            "main", provider="custom", smtp_host="127.0.0.1", smtp_port=port, smtp_security="none",
            from_email="news@example.com", password="app-pass", rate_per_minute=6000,
        )  # fmt: skip
        for name in ("a", "b", "c", "d"):
            bot.contacts.add(f"{name}@test.org", first_name=name, lists=["l"])
        cid = bot.campaigns.create(
            "C", account="main", lists=["l"], subject="Hi {{ first_name }}",
            html="<p>{{ unsubscribe_url }}</p>",
        ).id  # fmt: skip
        bot.campaigns.send_now(cid)
        for _ in range(3):
            bot.dispatcher.tick()

        campaign = bot.campaigns.get(cid)
        stats = bot.campaigns.stats(cid)
        assert campaign.status is CampaignStatus.PAUSED
        assert "24 hours" in (campaign.pause_reason or "")
        assert len(attempts) == 1  # no hammering: more attempts would extend the ban
        assert (stats.pending, stats.failed, stats.sent) == (4, 0, 0)  # nobody lost


def test_account_password_command_verifies_before_saving(server, cli_env) -> None:  # type: ignore[no-untyped-def]
    from click.testing import CliRunner

    from mailbot.cli import cli

    _, port = server
    runner = CliRunner()
    assert runner.invoke(cli, add_args(port), input="app-pass\n").exit_code == 0

    bad = runner.invoke(cli, ["account-password", "main"], input="typo\n")
    # CliRunner hands us the exception; in real use main() prints it as "Ошибка: ..."
    assert bad.exit_code == 1 and "new password failed" in str(bad.exception)
    assert "typo" not in bad.output and "typo" not in str(bad.exception)

    # the stored password still works: a no-op re-add of the same password verifies and saves
    good = runner.invoke(cli, ["account-password", "main"], input="app-pass\n")
    assert good.exit_code == 0 and "обновлён" in good.output


def test_failed_yandex_login_prints_the_checklist(cli_env) -> None:  # type: ignore[no-untyped-def]
    """Unreachable server stands in for any failed login; the Yandex hint must come with it."""
    from click.testing import CliRunner

    from mailbot.cli import cli

    result = CliRunner().invoke(
        cli,
        ["account-add", "ya", "--provider", "yandex", "--email", "me@yandex.ru",
         "--smtp-host", "127.0.0.1", "--smtp-port", "1"],
        input="pw\n",
    )  # fmt: skip
    assert result.exit_code == 1
    assert "ящик не сохранён" in result.output
    for needle in ("пароль приложения", "Пароли приложений и OAuth-токены", "EULA", "29.06.2026"):
        assert needle in result.output
