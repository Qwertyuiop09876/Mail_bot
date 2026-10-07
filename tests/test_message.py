from __future__ import annotations

import email
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage

from mailbot.message import build_message


def build(**kw: object) -> EmailMessage:
    args: dict[str, object] = {
        "from_email": "news@shop.ru",
        "from_name": "Магазин «Весна»",
        "to_email": "anna@mail.ru",
        "to_name": "Анна Иванова",
        "subject": "Скидки до 50% — только сегодня",
        "html": "<p>Привет</p>",
        "text": "Привет",
        "unsubscribe_url": "https://shop.ru/u/abc",
        "date": datetime(2026, 10, 7, 9, 0, tzinfo=UTC),
    }
    return build_message(**{**args, **kw})  # type: ignore[arg-type]


def roundtrip(msg: EmailMessage) -> EmailMessage:
    """Serialise exactly as SMTP would, then parse back."""
    return email.message_from_bytes(msg.as_bytes(), policy=policy.default)


def test_cyrillic_headers_survive_the_wire_and_are_ascii_encoded() -> None:
    msg = build()
    raw = msg.as_bytes()
    header_block = raw.split(b"\r\n\r\n", 1)[0]
    header_block.decode("ascii")  # raises if any raw 8-bit slipped into headers
    parsed = roundtrip(msg)
    assert parsed["Subject"] == "Скидки до 50% — только сегодня"
    assert parsed["From"].addresses[0].display_name == "Магазин «Весна»"
    assert parsed["From"].addresses[0].addr_spec == "news@shop.ru"
    assert parsed["To"].addresses[0].addr_spec == "anna@mail.ru"


def test_multipart_alternative_with_text_first_then_html() -> None:
    parsed = roundtrip(build(text="Привет, мир", html="<p>Привет, мир</p>"))
    assert parsed.get_content_type() == "multipart/alternative"
    kinds = [p.get_content_type() for p in parsed.iter_parts()]
    assert kinds == ["text/plain", "text/html"]
    assert parsed.get_body(("plain",)).get_content().strip() == "Привет, мир"  # type: ignore[union-attr]


def test_bulk_headers() -> None:
    msg = build()
    assert msg["List-Unsubscribe"] == "<https://shop.ru/u/abc>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["Precedence"] == "bulk"
    assert msg["Message-ID"].endswith("@shop.ru>")
    assert msg["Date"] == "Wed, 07 Oct 2026 09:00:00 +0000"


def test_one_click_header_only_for_https() -> None:
    msg = build(unsubscribe_url="http://localhost:8080/u/abc")
    assert msg["List-Unsubscribe"] == "<http://localhost:8080/u/abc>"
    assert msg["List-Unsubscribe-Post"] is None


def test_no_unsubscribe_headers_when_not_requested() -> None:
    msg = build(unsubscribe_url=None)
    assert msg["List-Unsubscribe"] is None and msg["List-Unsubscribe-Post"] is None


def test_reply_to_and_unique_message_ids() -> None:
    a, b = build(reply_to="support@shop.ru"), build()
    assert a["Reply-To"] == "support@shop.ru"
    assert a["Message-ID"] != b["Message-ID"]
