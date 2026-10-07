"""Operational command line: run the worker, serve unsubscribe links, health checks.

This is deliberately *not* a management interface — contacts, templates and campaigns are managed
through the Python API (see ``examples/quickstart.py``). The CLI covers what has to run as a
process or be checked from a shell.
"""

from __future__ import annotations

import logging
import signal
import threading
from wsgiref.simple_server import WSGIRequestHandler, make_server

import click

from . import __version__
from .app import MailBot
from .config import Settings
from .crypto import generate_key
from .dnscheck import check_domain
from .errors import MailbotError
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
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
    except click.Abort:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
