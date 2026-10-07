from __future__ import annotations

import pytest

from mailbot.errors import ValidationError
from mailbot.rendering import build_context, check_syntax, html_to_text, render


def ctx(**kw: object) -> dict[str, object]:
    base = {
        "email": "a@b.ru",
        "first_name": "Анна",
        "last_name": "Иванова",
        "attributes": {},
        "unsubscribe_url": "https://x/u",
    }
    return build_context(**{**base, **kw})  # type: ignore[arg-type]


def test_renders_all_parts_and_autogenerates_text() -> None:
    out = render(
        subject="Привет, {{ first_name }}",
        html=(
            "<h1>Здравствуйте, {{ name }}</h1>"
            "<p>Ссылка: <a href='{{ unsubscribe_url }}'>отписка</a></p>"
        ),
        text=None,
        context=ctx(),
    )
    assert out.subject == "Привет, Анна"
    assert "Здравствуйте, Анна Иванова" in out.html
    assert "Здравствуйте, Анна Иванова" in out.text
    assert "отписка (https://x/u)" in out.text


def test_missing_variable_is_an_error_not_a_blank() -> None:
    with pytest.raises(ValidationError, match="city"):
        render(subject="s", html="<p>{{ city }}</p>", text=None, context=ctx())


def test_default_filter_makes_a_variable_optional() -> None:
    out = render(
        subject="s", html="<p>{{ city | default('ваш город') }}</p>", text=None, context=ctx()
    )
    assert "ваш город" in out.html


def test_html_is_autoescaped_but_text_is_not() -> None:
    context = ctx(first_name="<b>Hax&</b>")
    out = render(
        subject="{{ first_name }}",
        html="<p>{{ first_name }}</p>",
        text="{{ first_name }}",
        context=context,
    )
    assert "&lt;b&gt;Hax&amp;&lt;/b&gt;" in out.html
    assert out.text == "<b>Hax&</b>"


def test_newlines_in_subject_cannot_inject_headers() -> None:
    out = render(
        subject="Hi {{ first_name }}",
        html="<p>x</p>",
        text=None,
        context=ctx(first_name="A\r\nBcc: evil@x.ru"),
    )
    assert "\n" not in out.subject and "\r" not in out.subject


def test_sandbox_blocks_attribute_escapes() -> None:
    with pytest.raises(ValidationError):
        render(subject="s", html="{{ ''.__class__.__mro__ }}", text=None, context=ctx())


def test_syntax_errors_are_reported_early() -> None:
    with pytest.raises(ValidationError, match="syntax error in html"):
        check_syntax(subject="s", html="{% if %}", text=None)


def test_builtin_variables_win_over_custom_attributes() -> None:
    context = ctx(attributes={"email": "spoof@x.ru", "city": "Тверь"})
    assert context["email"] == "a@b.ru" and context["city"] == "Тверь"


def test_html_to_text_handles_structure() -> None:
    text = html_to_text(
        "<style>p{}</style><p>Один</p><ul><li>раз</li><li>два</li></ul><p>A&nbsp;&amp;&nbsp;B<br>конец</p>"
    )
    assert "p{}" not in text
    assert "- раз" in text and "- два" in text
    assert "A & B\nконец" in text.replace("\xa0", " ")


def test_missing_name_is_undefined_so_default_filter_works_and_bare_use_is_caught() -> None:
    nameless = ctx(first_name=None, last_name=None)
    assert "first_name" not in nameless and "name" not in nameless
    out = render(
        subject="Hi {{ first_name | default('друг') }}",
        html="<p>x</p>",
        text=None,
        context=nameless,
    )
    assert out.subject == "Hi друг"
    with pytest.raises(ValidationError, match="first_name"):
        render(subject="Hi {{ first_name }}", html="<p>x</p>", text=None, context=nameless)
    # last name only: "name" is just the last name, first_name stays undefined
    assert ctx(first_name=None, last_name="Иванова")["name"] == "Иванова"
