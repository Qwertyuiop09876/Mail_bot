"""Template rendering (Jinja2 sandbox) and HTML → plain-text conversion.

Undefined variables are errors, not silent blanks: a campaign must never go out saying
"Hello, !". Use ``{{ first_name | default('друг') }}`` for optional values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from .errors import ValidationError

_html_env = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
_text_env = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)


@dataclass(frozen=True)
class Rendered:
    subject: str
    html: str
    text: str


def check_syntax(*, subject: str, html: str, text: str | None) -> None:
    """Raise ValidationError if any part is not a valid Jinja template."""
    for label, source, env in (
        ("subject", subject, _text_env),
        ("html", html, _html_env),
        ("text", text, _text_env),
    ):
        if source is None:
            continue
        try:
            env.parse(source)
        except TemplateError as exc:
            raise ValidationError(f"Template syntax error in {label}: {exc}") from exc


def render(*, subject: str, html: str, text: str | None, context: dict[str, Any]) -> Rendered:
    try:
        out_subject = _text_env.from_string(subject).render(context)
        out_html = _html_env.from_string(html).render(context)
        out_text = _text_env.from_string(text).render(context) if text else html_to_text(out_html)
    except TemplateError as exc:
        raise ValidationError(f"Cannot render template: {exc}") from exc
    # A newline in a header would let contact data inject headers; collapse all whitespace.
    out_subject = " ".join(out_subject.split())
    if not out_subject:
        raise ValidationError("Rendered subject is empty")
    return Rendered(subject=out_subject, html=out_html, text=out_text)


_BLOCK_TAGS = {
    "p", "div", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
    "blockquote", "section", "article", "header", "footer",
}  # fmt: skip
_SKIP_TAGS = {"script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._href: str | None = None
        self._link_text_start = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text_start = len(self.parts)
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(alt)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag == "a" and self._href:
            link_text = "".join(self.parts[self._link_text_start :]).strip()
            href = self._href
            self._href = None
            if href.startswith(("http://", "https://")) and href != link_text:
                self.parts.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    """Readable plain-text alternative for an HTML body (links are kept as ``text (url)``)."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def build_context(
    *,
    email: str,
    first_name: str | None,
    last_name: str | None,
    attributes: dict[str, Any],
    unsubscribe_url: str,
) -> dict[str, Any]:
    """Variables available in templates. Built-ins win over same-named custom attributes.

    ``first_name``, ``last_name`` and ``name`` exist only when the contact actually has them, so
    ``{{ first_name }}`` fails validation for a nameless contact (no "Hello, !" mailings) while
    ``{{ first_name | default('друг') }}`` falls back as expected.
    """
    context: dict[str, Any] = dict(attributes)
    for key, value in (
        ("first_name", first_name),
        ("last_name", last_name),
        ("name", " ".join(p for p in (first_name, last_name) if p)),
    ):
        if value:
            context[key] = value
        else:
            context.pop(key, None)  # a custom attribute must not stand in for a missing name
    context["email"] = email
    context["unsubscribe_url"] = unsubscribe_url
    return context
