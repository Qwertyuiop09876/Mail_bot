from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Any

import pytest

from mailbot.crypto import SecretBox, generate_key
from mailbot.errors import ConfigError
from mailbot.models import ContactStatus
from mailbot.unsubscribe import UnsubscribeSigner

from .conftest import BASE_URL, Env


def call(app: Callable[..., Any], method: str, path: str) -> tuple[str, bytes]:
    status: list[str] = []
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "wsgi.input": BytesIO(b"List-Unsubscribe=One-Click"),
        "CONTENT_LENGTH": "26",
    }
    body = b"".join(app(environ, lambda s, _h: status.append(s)))
    return status[0], body


def test_secretbox_roundtrip_and_wrong_key() -> None:
    box = SecretBox(generate_key())
    assert box.decrypt(box.encrypt("пароль")) == "пароль"
    with pytest.raises(ConfigError, match="differs"):
        SecretBox(generate_key()).decrypt(box.encrypt("x"))
    with pytest.raises(ConfigError, match="valid Fernet key"):
        SecretBox("not-a-key")


def test_tokens_are_signed_and_tamper_proof() -> None:
    signer = UnsubscribeSigner(SecretBox(generate_key()), BASE_URL)
    url = signer.make_url(42, 7)
    assert url.startswith(BASE_URL + "/42.7.")
    token = url.rsplit("/", 1)[1]
    parsed = signer.parse(token)
    assert parsed is not None and (parsed.contact_id, parsed.campaign_id) == (42, 7)
    assert signer.parse(token.replace("42.7", "43.7")) is None
    assert signer.parse(token[:-2] + "xx") is None
    assert signer.parse("garbage") is None
    other = UnsubscribeSigner(SecretBox(generate_key()), BASE_URL)
    assert other.parse(token) is None  # signed with a different key


def test_get_only_confirms_post_unsubscribes(env: Env) -> None:
    env.bot.contacts.add("anna@mail.ru")
    contact = env.bot.contacts.get("anna@mail.ru")
    assert env.bot.signer is not None
    path = "/unsubscribe/" + env.bot.signer.make_url(contact.id, None).rsplit("/", 1)[1]
    app = env.bot.unsubscribe_app()

    status, body = call(app, "GET", path)  # link scanners do this
    assert status.startswith("200") and "anna@mail.ru" in body.decode()
    assert env.bot.contacts.get("anna@mail.ru").status is ContactStatus.ACTIVE

    status, body = call(app, "POST", path)  # one-click / button
    assert status.startswith("200") and "Вы отписаны" in body.decode()
    assert env.bot.contacts.get("anna@mail.ru").status is ContactStatus.UNSUBSCRIBED
    assert call(app, "POST", path)[0].startswith("200")  # idempotent


def test_bad_tokens_get_404_and_change_nothing(env: Env) -> None:
    env.bot.contacts.add("anna@mail.ru")
    app = env.bot.unsubscribe_app()
    for path in ("/unsubscribe/1.0.forged", "/unsubscribe/abc", "/other/1.0.x", "/unsubscribe/"):
        assert call(app, "POST", path)[0].startswith("404")
    assert env.bot.contacts.get("anna@mail.ru").status is ContactStatus.ACTIVE


def test_unsubscribe_link_in_a_real_message_works_end_to_end(env: Env) -> None:
    cid = env.seed(2)
    env.bot.campaigns.send_now(cid)
    env.run_to_completion()
    recipient, message = env.transport.sent[0]
    link = message["List-Unsubscribe"].strip("<>")
    assert call(env.bot.unsubscribe_app(), "POST", "/unsubscribe/" + link.rsplit("/", 1)[1])[
        0
    ].startswith("200")
    contact = env.bot.contacts.get(recipient)
    assert contact.status is ContactStatus.UNSUBSCRIBED and contact.status_campaign_id == cid
