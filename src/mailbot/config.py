"""Runtime settings, read from ``MAILBOT_*`` environment variables and an optional ``.env``."""

from __future__ import annotations

from datetime import tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAILBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///data/mailbot.db"
    secret_key: SecretStr | None = None
    unsubscribe_base_url: str | None = None
    timezone: str = "Europe/Moscow"

    poll_interval_seconds: float = 5.0
    max_attempts: int = 3
    retry_base_seconds: int = 300
    smtp_timeout_seconds: float = 30.0
    # Consecutive transient/rejected sends after which the dispatcher stops hammering the server.
    circuit_breaker_threshold: int = 3
    # A delivery stuck in "sending" for longer than this was interrupted by a crash.
    stale_sending_seconds: int = 900
    log_level: str = "INFO"

    @field_validator("unsubscribe_base_url")
    @classmethod
    def _strip_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            return None
        if not v.startswith(("https://", "http://")):
            raise ValueError("must start with https:// (or http:// for local development)")
        return v

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @property
    def tz(self) -> tzinfo:
        return ZoneInfo(self.timezone)

    def require_secret_key(self) -> str:
        if self.secret_key is None or not self.secret_key.get_secret_value():
            raise ConfigError(
                "MAILBOT_SECRET_KEY is not set. Generate one with `mailbot gen-key` "
                "and put it into .env."
            )
        return self.secret_key.get_secret_value()

    def require_unsubscribe_base_url(self) -> str:
        if not self.unsubscribe_base_url:
            raise ConfigError(
                "MAILBOT_UNSUBSCRIBE_BASE_URL is not set. Bulk mail must carry a working "
                "unsubscribe link, so campaigns cannot be sent without it."
            )
        return self.unsubscribe_base_url
