"""Signed unsubscribe links and the minimal WSGI endpoint that serves them.

The link carries ``contact_id.campaign_id.signature``; the signature is an HMAC, so tokens can't
be forged or enumerated. A GET only shows a confirmation form — mail scanners and link prefetchers
GET every URL in a message and must not unsubscribe people. The actual unsubscribe happens on POST,
which is also exactly what RFC 8058 one-click clients (Gmail, Yahoo, Mail.ru, ...) send.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select

from .crypto import SecretBox
from .db import Database
from .models import Contact

log = logging.getLogger(__name__)

PAGE_TITLE = "Отписка от рассылки"
_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;"
    "color:#222}button{font-size:1rem;padding:.6rem 1.2rem;cursor:pointer}"
)


@dataclass(frozen=True)
class UnsubscribeToken:
    contact_id: int
    campaign_id: int | None


class UnsubscribeSigner:
    def __init__(self, box: SecretBox, base_url: str) -> None:
        self._box = box
        self._base = base_url.rstrip("/")

    @staticmethod
    def _payload(contact_id: int, campaign_id: int | None) -> str:
        return f"{contact_id}.{campaign_id if campaign_id is not None else 0}"

    def make_url(self, contact_id: int, campaign_id: int | None) -> str:
        payload = self._payload(contact_id, campaign_id)
        return f"{self._base}/{payload}.{self._box.sign('unsubscribe:' + payload)}"

    def parse(self, token: str) -> UnsubscribeToken | None:
        parts = token.split(".")
        if len(parts) != 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            return None
        contact_id, campaign_id = int(parts[0]), int(parts[1])
        payload = self._payload(contact_id, campaign_id or None)
        if not self._box.verify("unsubscribe:" + payload, parts[2]):
            return None
        return UnsubscribeToken(contact_id, campaign_id or None)


def _page(body: str) -> bytes:
    doc = (
        "<!doctype html><html lang=ru><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{PAGE_TITLE}</title>"
        f"<style>{_STYLE}</style>{body}</html>"
    )
    return doc.encode("utf-8")


class UnsubscribeApp:
    """WSGI application. Mount it at the path of ``MAILBOT_UNSUBSCRIBE_BASE_URL``."""

    def __init__(
        self,
        db: Database,
        signer: UnsubscribeSigner,
        base_url: str,
        on_unsubscribe: Callable[[str, int | None], None],
    ) -> None:
        self._db = db
        self._signer = signer
        self._prefix = urlparse(base_url).path.rstrip("/") + "/"
        self._on_unsubscribe = on_unsubscribe

    def __call__(
        self, environ: dict[str, object], start_response: Callable[..., object]
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = str(environ.get("PATH_INFO", ""))
        token = (
            self._signer.parse(path[len(self._prefix) :]) if path.startswith(self._prefix) else None
        )
        with self._db.session() as s:
            email = (
                s.scalar(select(Contact.email).where(Contact.id == token.contact_id))
                if token
                else None
            )
        if token is None or email is None:
            return self._respond(start_response, "404 Not Found", "<h1>Ссылка недействительна</h1>")

        if method in ("GET", "HEAD"):
            body = (
                f"<h1>{PAGE_TITLE}</h1><p>Адрес: <b>{html.escape(email)}</b></p>"
                "<form method=post><button type=submit>Отписаться</button></form>"
            )
            return self._respond(start_response, "200 OK", body, head=method == "HEAD")
        if method == "POST":
            self._on_unsubscribe(email, token.campaign_id)
            log.info("unsubscribed %s via link (campaign %s)", email, token.campaign_id)
            body = (
                f"<h1>Вы отписаны</h1><p>Адрес {html.escape(email)} "
                "больше не получит наши рассылки.</p>"
            )
            return self._respond(start_response, "200 OK", body)
        return self._respond(
            start_response, "405 Method Not Allowed", "<h1>Метод не поддерживается</h1>"
        )

    @staticmethod
    def _respond(
        start_response: Callable[..., object], status: str, body: str, *, head: bool = False
    ) -> list[bytes]:
        payload = _page(body)
        start_response(
            status,
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(payload))),
                ("Cache-Control", "no-store"),
                ("X-Robots-Tag", "noindex"),
            ],
        )
        return [] if head else [payload]
