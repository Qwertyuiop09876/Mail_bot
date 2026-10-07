"""Reusable message templates. A campaign copies a template's content when it is created."""

from __future__ import annotations

from sqlalchemy import select

from .db import Database
from .errors import NotFoundError, ValidationError
from .models import Template
from .rendering import check_syntax


class TemplateService:
    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, name: str, *, subject: str, html: str, text: str | None = None) -> Template:
        """Create the template or overwrite the existing one with the same name."""
        name = name.strip()
        if not name:
            raise ValidationError("Template name must not be empty")
        if not subject.strip():
            raise ValidationError("Template subject must not be empty")
        if not html.strip():
            raise ValidationError("Template html must not be empty")
        check_syntax(subject=subject, html=html, text=text)
        with self._db.session() as s:
            tpl = s.scalar(select(Template).where(Template.name == name))
            if tpl is None:
                tpl = Template(name=name, subject=subject, html=html, text=text)
                s.add(tpl)
            else:
                tpl.subject, tpl.html, tpl.text = subject, html, text
            s.flush()
            return tpl

    def get(self, name: str) -> Template:
        with self._db.session() as s:
            tpl = s.scalar(select(Template).where(Template.name == name))
            if tpl is None:
                raise NotFoundError(f"Template {name!r} not found")
            return tpl

    def all(self) -> list[Template]:
        with self._db.session() as s:
            return list(s.scalars(select(Template).order_by(Template.name)))

    def delete(self, name: str) -> None:
        with self._db.session() as s:
            tpl = s.scalar(select(Template).where(Template.name == name))
            if tpl is None:
                raise NotFoundError(f"Template {name!r} not found")
            s.delete(tpl)  # campaigns keep their own copy; their template_id becomes NULL
