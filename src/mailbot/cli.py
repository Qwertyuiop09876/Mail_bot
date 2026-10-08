"""Operational command line: run the worker, serve unsubscribe links, health checks.

This is deliberately *not* a management interface — contacts, templates and campaigns are managed
through the Python API (see ``examples/quickstart.py``). The CLI covers what has to run as a
process or be checked from a shell.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from wsgiref.simple_server import WSGIRequestHandler, make_server

import click
from sqlalchemy.exc import SQLAlchemyError

from . import __version__
from .app import MailBot
from .config import Settings
from .crypto import generate_key
from .dnscheck import check_domain
from .errors import LoginFailedError, MailbotError
from .models import CampaignStatus


def _bot() -> MailBot:
    """Use as ``with _bot() as bot:`` so the database connection is always released."""
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return MailBot(settings)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="mailbot")
def cli() -> None:
    """mailbot — рассылки через Яндекс Почту и доменную почту."""


@cli.command("gen-key")
def gen_key() -> None:
    """Сгенерировать MAILBOT_SECRET_KEY (шифрование паролей и подпись ссылок отписки)."""
    click.echo(generate_key())


@cli.command("init-db")
def init_db() -> None:
    """Создать или обновить схему базы данных (миграции)."""
    with _bot() as bot:
        bot.init_db()
        click.echo(f"База готова: {bot.settings.database_url}")


_YANDEX_LOGIN_HINT = """\
Что проверить для Яндекса:
 1. Нужен пароль приложения (тип «Почта»), а не пароль аккаунта. Новый пароль иногда начинает
    работать не сразу — подождите до 2–3 часов.
 2. Почта → Все настройки → Почтовые программы: включите «С сервера imap.yandex.ru по протоколу
    IMAP» и «Пароли приложений и OAuth-токены».
 3. Новый ящик один раз откройте в браузере и примите пользовательское соглашение
    (ошибка «Please accept EULA first»).
 4. Логин: для ящика на своём домене — полный адрес; для личного @yandex.ru попробуйте и полный
    адрес, и имя до @ (--login имя).
 5. Ящик на своём домене: по сообщениям СМИ и Saby, с 29.06.2026 доступ по IMAP/SMTP требует
    платного тарифа Яндекс 360. Официально это проверить не удалось — если пункты 1–4 в порядке,
    начните с этого.
 6. Защита Яндекса могла временно заблокировать аккаунт (чаще — нет привязанного телефона);
    блокировка обычно снимается через пару часов."""


@cli.command("account-add")
@click.argument("name")
@click.option(
    "--provider",
    type=click.Choice(["yandex", "yandex360", "custom"]),
    required=True,
    help="yandex — обычный ящик @yandex.ru; yandex360 — ящик на своём домене в Яндекс 360; "
    "custom — любой другой SMTP.",
)
@click.option("--email", "from_email", required=True, help="Адрес отправителя.")
@click.option("--from-name", help="Имя отправителя, как увидит получатель.")
@click.option("--reply-to", help="Куда приходят ответы, если не на адрес отправителя.")
@click.option("--login", "username", help="Логин SMTP, если он отличается от адреса.")
@click.option("--smtp-host", help="Для custom обязателен.")
@click.option("--smtp-port", type=int)
@click.option("--smtp-security", type=click.Choice(["ssl", "starttls", "none"]))
@click.option("--imap-host", help="Нужен для чтения отчётов о недоставке (scan-bounces).")
@click.option("--imap-port", type=int)
@click.option("--daily-limit", type=int, help="Писем в сутки (по умолчанию — из пресета).")
@click.option("--rate-per-minute", type=int, help="Писем в минуту (по умолчанию — из пресета).")
@click.option(
    "--password-env",
    metavar="VAR",
    help="Взять пароль из переменной окружения VAR вместо запроса. Пароль не принимается "
    "аргументом: он остался бы в истории shell и в списке процессов.",
)
@click.option("--no-check", is_flag=True, help="Сохранить без проверки входа в SMTP.")
def account_add(
    name: str,
    provider: str,
    from_email: str,
    from_name: str | None,
    reply_to: str | None,
    username: str | None,
    smtp_host: str | None,
    smtp_port: int | None,
    smtp_security: str | None,
    imap_host: str | None,
    imap_port: int | None,
    daily_limit: int | None,
    rate_per_minute: int | None,
    password_env: str | None,
    no_check: bool,
) -> None:
    """Добавить ящик-отправитель. Пароль спросят скрытым вводом.

    Сначала выполняется вход в SMTP, и только если он удался, ящик сохраняется (при --no-check
    проверки нет). Пароль хранится в базе в зашифрованном виде; база и .env в git не попадают.
    """
    if password_env:
        password = os.environ.get(password_env, "")
        if not password:
            raise click.UsageError(f"Переменная окружения {password_env} не задана или пуста")
    else:
        password = click.prompt(
            "Пароль (для Яндекса — пароль приложения), ввод скрыт", hide_input=True
        )

    with _bot() as bot:
        bot.init_db()
        try:
            # verify=True logs in BEFORE anything is stored: a failure leaves no account behind
            account = bot.accounts.add(
                name,
                provider=provider,
                from_email=from_email,
                password=password,
                from_name=from_name,
                reply_to=reply_to,
                username=username,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_security=smtp_security,
                imap_host=imap_host,
                imap_port=imap_port,
                daily_limit=daily_limit,
                rate_per_minute=rate_per_minute,
                verify=not no_check,
            )
        except LoginFailedError as exc:
            click.secho(f"Вход не удался, ящик не сохранён: {exc}", fg="red", err=True)
            if provider.startswith("yandex"):
                click.echo(_YANDEX_LOGIN_HINT, err=True)
            raise SystemExit(1) from exc
        for warning in bot.accounts.sender_warnings(account):
            click.secho(f"! {warning}", fg="yellow")
        if not no_check:
            click.secho(f"Вход в {account.smtp_host}:{account.smtp_port} выполнен", fg="green")
        click.echo(
            f"Ящик «{account.name}» ({account.from_email}) сохранён. "
            f"Лимиты: {account.daily_limit} писем/сутки, {account.rate_per_minute}/мин."
        )


@cli.command("account-password")
@click.argument("name")
@click.option("--password-env", metavar="VAR", help="Взять новый пароль из переменной окружения.")
@click.option("--no-check", is_flag=True, help="Сохранить без проверки входа.")
def account_password(name: str, password_env: str | None, no_check: bool) -> None:
    """Заменить пароль ящика (например, после отзыва пароля приложения).

    Новый пароль сначала проверяется входом в SMTP; при неудаче старый остаётся как был.
    """
    if password_env:
        password = os.environ.get(password_env, "")
        if not password:
            raise click.UsageError(f"Переменная окружения {password_env} не задана или пуста")
    else:
        password = click.prompt("Новый пароль, ввод скрыт", hide_input=True)
    with _bot() as bot:
        try:
            bot.accounts.set_password(name, password, verify=not no_check)
        except LoginFailedError:
            if bot.accounts.get(name).provider.startswith("yandex"):
                click.echo(_YANDEX_LOGIN_HINT, err=True)
            raise
        click.secho(f"Пароль ящика «{name}» обновлён", fg="green")


@cli.command()
def accounts() -> None:
    """Показать добавленные ящики (пароли не показываются)."""
    with _bot() as bot:
        rows = bot.accounts.all()
        if not rows:
            click.echo(
                "Ящиков нет. Добавьте: mailbot account-add NAME --provider yandex --email ..."
            )
            return
        for a in rows:
            click.echo(
                f"{a.name} [{a.provider}] {a.from_email} · "
                f"{a.smtp_host}:{a.smtp_port}/{a.smtp_security.value} · "
                f"{a.daily_limit}/сутки, {a.rate_per_minute}/мин"
            )


@cli.command()
@click.option("--once", is_flag=True, help="Сделать один проход и выйти (для cron).")
def run(once: bool) -> None:
    """Запустить воркер: отправляет запланированные кампании с учётом лимитов."""
    with _bot() as bot:
        bot.init_db()
        if once:
            report = bot.dispatcher.tick()
            click.echo(
                f"отправлено={report.sent} повторов={report.retried} ошибок={report.failed} "
                f"запущено={report.activated} завершено={report.completed}"
            )
            return
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        bot.run_forever(stop)


@cli.command("serve-unsubscribe")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
def serve_unsubscribe(host: str, port: int) -> None:
    """Запустить сервер ссылок отписки (за nginx/Caddy с HTTPS).

    Для нагрузки подойдёт любой WSGI-сервер: mailbot.unsubscribe.UnsubscribeApp.
    """

    class Quiet(WSGIRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            logging.getLogger("mailbot.http").info(format, *args)

    with (
        _bot() as bot,
        make_server(host, port, bot.unsubscribe_app(), handler_class=Quiet) as httpd,
    ):
        click.echo(f"Слушаю http://{host}:{port} (Ctrl+C для остановки)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            click.echo("Остановлено")


@cli.command("check-account")
@click.argument("name")
def check_account(name: str) -> None:
    """Проверить вход в SMTP для аккаунта (письма не отправляются)."""
    with _bot() as bot:
        result = bot.accounts.check_connection(name)
    for warning in result.warnings:
        click.secho(f"! {warning}", fg="yellow")
    click.secho(result.detail, fg="green" if result.ok else "red")
    raise SystemExit(0 if result.ok else 1)


@cli.command("scan-bounces")
@click.argument("account")
def scan_bounces(account: str) -> None:
    """Прочитать отчёты о недоставке (DSN) из ящика по IMAP и отписать «мёртвые» адреса."""
    with _bot() as bot:
        report = bot.bounces.scan(account)
    click.echo(
        f"писем просмотрено={report.scanned} жёстких={report.hard} мягких={report.soft} "
        f"не наши={report.unmatched}"
    )
    for address in report.suppressed:
        click.echo(f"  исключён: {address}")


@cli.command("check-domain")
@click.argument("domain")
@click.option(
    "--selector",
    "selectors",
    multiple=True,
    default=["mail"],
    show_default=True,
    help="DKIM-селектор (можно несколько раз).",
)
@click.option("--provider", type=click.Choice(["yandex", "yandex360", "custom"]), default=None)
def check_domain_cmd(domain: str, selectors: tuple[str, ...], provider: str | None) -> None:
    """Проверить SPF, DKIM и DMARC домена отправителя."""
    colors = {"ok": "green", "warn": "yellow", "error": "red"}
    findings = check_domain(domain, dkim_selectors=selectors, provider=provider)
    for f in findings:
        click.secho(f"[{f.level.upper():5}] {f.check}: {f.message}", fg=colors[f.level])
    raise SystemExit(1 if any(f.level == "error" for f in findings) else 0)


@cli.command()
def status() -> None:
    """Показать кампании и ход отправки."""
    with _bot() as bot:
        campaigns = bot.campaigns.all()
        if not campaigns:
            click.echo("Кампаний нет")
            return
        for c in campaigns:
            st = bot.campaigns.stats(c.id)
            when = (
                c.scheduled_at.astimezone(bot.settings.tz).strftime("%Y-%m-%d %H:%M")
                if c.scheduled_at
                else "—"
            )
            click.echo(
                f"#{c.id} [{c.status.value}] {c.name} · план: {when} · "
                f"отправлено {st.sent}/{st.total}, ошибок {st.failed}, пропущено {st.skipped}"
            )
            if c.status is CampaignStatus.PAUSED and c.pause_reason:
                click.secho(f"    пауза: {c.pause_reason}", fg="yellow")


def main() -> None:
    try:
        cli(standalone_mode=False)
    except MailbotError as exc:
        click.secho(f"Ошибка: {exc}", fg="red", err=True)
        raise SystemExit(1) from exc
    except SQLAlchemyError as exc:
        # Show the database's own wording only: the statement and its parameters can hold addresses.
        reason = str(getattr(exc, "orig", None) or type(exc).__name__)
        click.secho(
            f"Ошибка базы данных: {reason}. Если база новая — выполните `mailbot init-db`.",
            fg="red",
            err=True,
        )
        raise SystemExit(1) from exc
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
    except click.Abort:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
