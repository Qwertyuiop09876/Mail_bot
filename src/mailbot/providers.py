"""Connection presets for known mail providers.

Limits are *conservative defaults*, not guarantees: providers change them and may lower them
for senders they suspect of spamming. Override per account (``daily_limit``/``rate_per_minute``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError
from .models import Security


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    title: str
    smtp_host: str
    smtp_port: int
    smtp_security: Security
    imap_host: str | None
    imap_port: int | None
    daily_limit: int
    rate_per_minute: int
    notes: str


YANDEX = ProviderPreset(
    key="yandex",
    title="Яндекс Почта (обычный ящик @yandex.ru)",
    smtp_host="smtp.yandex.ru",
    smtp_port=465,
    smtp_security=Security.SSL,
    imap_host="imap.yandex.ru",
    imap_port=993,
    daily_limit=500,
    rate_per_minute=15,
    notes=(
        "Нужен пароль приложения (не пароль от аккаунта) и включённый доступ по IMAP/почтовым "
        "программам в настройках ящика. Адрес отправителя должен совпадать с логином."
    ),
)

YANDEX_360 = ProviderPreset(
    key="yandex360",
    title="Яндекс 360 (ящик на своём домене)",
    smtp_host="smtp.yandex.ru",
    smtp_port=465,
    smtp_security=Security.SSL,
    imap_host="imap.yandex.ru",
    imap_port=993,
    daily_limit=3000,
    rate_per_minute=20,
    notes=(
        "Логин — полный адрес ящика на вашем домене. Нужен пароль приложения. "
        "SPF/DKIM для домена настраиваются в Яндекс 360 (Администрирование → Домены)."
    ),
)

# Any other SMTP server (Mail.ru для бизнеса, хостинг-провайдер, свой Postfix, ...).
# Connection parameters must be passed explicitly.
CUSTOM = ProviderPreset(
    key="custom",
    title="Любой SMTP-сервер (доменная почта)",
    smtp_host="",
    smtp_port=465,
    smtp_security=Security.SSL,
    imap_host=None,
    imap_port=993,
    daily_limit=300,
    rate_per_minute=10,
    notes="Лимиты хостинга уточните у провайдера и задайте явно.",
)

PRESETS: dict[str, ProviderPreset] = {p.key: p for p in (YANDEX, YANDEX_360, CUSTOM)}


def get_preset(key: str) -> ProviderPreset:
    try:
        return PRESETS[key]
    except KeyError:
        raise ValidationError(
            f"Unknown provider {key!r}. Available: {', '.join(sorted(PRESETS))}"
        ) from None
