"""Полный сценарий от аккаунта до запланированной рассылки.

Запуск (после `make install`, `mailbot gen-key` → .env, см. README):

    YANDEX_APP_PASSWORD=... python examples/quickstart.py contacts.csv me@example.com

Скрипт только готовит и планирует кампанию. Отправляет её воркер: `mailbot run`.
Пароль берётся из переменной окружения — не вписывайте его в код.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from mailbot import MailBot

HTML = """\
<h1>Здравствуйте, {{ first_name | default('друг') }}!</h1>
<p>Рассказываем, что нового в октябре.</p>
<p style="color:#888;font-size:12px">
  Вы получили это письмо, потому что подписались на новости.
  <a href="{{ unsubscribe_url }}">Отписаться</a>
</p>
"""


def prepare_campaign(bot: MailBot, *, password: str, contacts_csv: Path, test_to: str) -> int:
    """Создаёт аккаунт, импортирует контакты, делает кампанию, шлёт тест и ставит в расписание."""
    bot.init_db()

    # Для ящика на своём домене вне Яндекса: provider="custom", smtp_host="smtp.вашхост.ru", ...
    bot.accounts.add(
        "main",
        provider="yandex360",
        from_email="news@example.com",
        from_name="Команда Example",
        password=password,
    )
    check = bot.accounts.check_connection("main")
    if not check.ok:
        raise SystemExit(f"Не удалось войти в SMTP: {check.detail}")

    report = bot.contacts.import_csv(contacts_csv, list_name="clients")
    print(f"Контакты: +{report.added}, обновлено {report.updated}, ошибок {len(report.invalid)}")

    bot.templates.save(
        "october", subject="Новости октября, {{ first_name | default('друг') }}", html=HTML
    )
    campaign = bot.campaigns.create(
        "Октябрьская рассылка", account="main", lists=["clients"], template="october"
    )

    validation = bot.campaigns.validate(campaign.id)
    for warning in validation.warnings:
        print("Внимание:", warning)
    print(f"Получателей: {validation.recipients}")

    bot.dispatcher.send_test(campaign.id, test_to)  # посмотрите письмо глазами получателя

    # Время без часового пояса трактуется в MAILBOT_TIMEZONE (по умолчанию Europe/Moscow).
    bot.campaigns.schedule(campaign.id, datetime.now() + timedelta(days=1))
    return campaign.id


def main() -> None:
    contacts_csv, test_to = Path(sys.argv[1]), sys.argv[2]
    with MailBot() as bot:
        campaign_id = prepare_campaign(
            bot,
            password=os.environ["YANDEX_APP_PASSWORD"],
            contacts_csv=contacts_csv,
            test_to=test_to,
        )
    print(f"Кампания #{campaign_id} запланирована. Запустите воркер: mailbot run")


if __name__ == "__main__":
    main()
