"""The public entry point: wires all services together.

from mailbot import MailBot

bot = MailBot()            # reads MAILBOT_* settings / .env
bot.init_db()
bot.accounts.add("main", provider="yandex360", from_email="news@example.com", password="...")
...
bot.run_forever()          # worker: sends scheduled campaigns
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from .accounts import AccountService
from .bounces import BounceScanner
from .campaigns import CampaignService
from .config import Settings
from .contacts import ContactService
from .crypto import SecretBox
from .db import Database
from .dispatcher import Dispatcher
from .models import utcnow
from .smtp import SmtpConfig, SmtpTransport, Transport
from .templates import TemplateService
from .unsubscribe import UnsubscribeApp, UnsubscribeSigner


class MailBot:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport_factory: Callable[[SmtpConfig], Transport] = SmtpTransport,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        """``transport_factory``, ``clock`` and ``sleep`` exist for tests; leave them alone."""
        self.settings = settings or Settings()
        self.db = Database(self.settings.database_url)
        self._box = SecretBox(self.settings.require_secret_key())
        base_url = self.settings.unsubscribe_base_url
        self.signer = UnsubscribeSigner(self._box, base_url) if base_url else None

        self.accounts = AccountService(self.db, self.settings, self._box, transport_factory)
        self.contacts = ContactService(self.db)
        self.templates = TemplateService(self.db)
        self.campaigns = CampaignService(self.db, self.settings, self.signer)
        self.bounces = BounceScanner(self.db, self.contacts, self.accounts)
        self.dispatcher = Dispatcher(
            self.db,
            self.settings,
            self.accounts,
            self.signer,
            transport_factory=transport_factory,
            clock=clock,
            sleep=sleep,
        )

    def init_db(self) -> None:
        """Create or upgrade the database schema (safe to call on every start)."""
        self.db.upgrade()

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Run the sending worker until ``stop`` is set."""
        self.dispatcher.run_forever(stop)

    def unsubscribe_app(self) -> UnsubscribeApp:
        """WSGI app for the unsubscribe links (``mailbot serve-unsubscribe`` or any WSGI server)."""
        base_url = self.settings.require_unsubscribe_base_url()
        assert self.signer is not None
        return UnsubscribeApp(
            self.db,
            self.signer,
            base_url,
            self._unsubscribe_via_link,
        )

    def _unsubscribe_via_link(self, email: str, campaign_id: int | None) -> None:
        self.contacts.unsubscribe(email, reason="unsubscribed via link", campaign_id=campaign_id)

    def close(self) -> None:
        self.db.dispose()

    def __enter__(self) -> MailBot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
