"""Contacts, mailing lists and the suppression rules attached to them.

Suppression rule: a contact that is not ``active`` (unsubscribed / bounced / complained) is never
mailed, and importing or re-adding the address never reactivates it. Only an explicit
:meth:`ContactService.resubscribe` does.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .db import Database
from .emails import normalize_email
from .errors import NotFoundError, StateError, ValidationError
from .models import (
    Campaign,
    CampaignStatus,
    Contact,
    ContactStatus,
    MailingList,
    campaign_lists,
    list_members,
    utcnow,
)

_EMAIL_HEADERS = {"email", "e-mail", "mail", "почта", "эл. почта", "электронная почта"}
_FIRST_NAME_HEADERS = {"first_name", "firstname", "first name", "name", "имя"}
_LAST_NAME_HEADERS = {"last_name", "lastname", "last name", "surname", "фамилия"}
_ACTIVE_CAMPAIGN_STATES = (
    CampaignStatus.DRAFT,
    CampaignStatus.SCHEDULED,
    CampaignStatus.SENDING,
    CampaignStatus.PAUSED,
)


@dataclass
class ImportReport:
    added: int = 0
    updated: int = 0
    already_suppressed: int = 0  # unsubscribed/bounced: kept suppressed, not reactivated
    duplicates_in_file: int = 0
    invalid: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason)

    @property
    def total_rows(self) -> int:
        return (
            self.added
            + self.updated
            + self.already_suppressed
            + self.duplicates_in_file
            + len(self.invalid)
        )


@dataclass(frozen=True)
class ListInfo:
    id: int
    name: str
    description: str | None
    total: int
    active: int


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


class ContactService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- lists -----------------------------------------------------------------------------

    def create_list(self, name: str, description: str | None = None) -> MailingList:
        name = name.strip()
        if not name:
            raise ValidationError("List name must not be empty")
        with self._db.session() as s:
            if s.scalar(select(MailingList).where(MailingList.name == name)):
                raise ValidationError(f"List {name!r} already exists")
            obj = MailingList(name=name, description=description)
            s.add(obj)
            s.flush()
            return obj

    def get_list(self, name: str) -> MailingList:
        with self._db.session() as s:
            return self._list(s, name)

    def lists(self) -> list[ListInfo]:
        with self._db.session() as s:
            rows = s.execute(
                select(
                    MailingList,
                    func.count(Contact.id),
                    func.count(Contact.id).filter(Contact.status == ContactStatus.ACTIVE),
                )
                .outerjoin(list_members, list_members.c.list_id == MailingList.id)
                .outerjoin(Contact, Contact.id == list_members.c.contact_id)
                .group_by(MailingList.id)
                .order_by(MailingList.name)
            ).all()
            return [
                ListInfo(lst.id, lst.name, lst.description, total, active)
                for lst, total, active in rows
            ]

    def delete_list(self, name: str) -> None:
        """Delete a list (contacts stay). Refused while an unfinished campaign targets it."""
        with self._db.session() as s:
            lst = self._list(s, name)
            in_use = s.scalar(
                select(func.count())
                .select_from(campaign_lists)
                .join(Campaign, Campaign.id == campaign_lists.c.campaign_id)
                .where(
                    campaign_lists.c.list_id == lst.id,
                    Campaign.status.in_(_ACTIVE_CAMPAIGN_STATES),
                )
            )
            if in_use:
                raise StateError(f"List {name!r} is used by {in_use} unfinished campaign(s)")
            # Finished campaigns keep their history, so detach the list from them first.
            s.execute(delete(campaign_lists).where(campaign_lists.c.list_id == lst.id))
            s.delete(lst)

    def add_to_list(self, list_name: str, emails: Iterable[str]) -> int:
        """Add existing contacts to a list. Returns how many memberships were created."""
        with self._db.session() as s:
            lst = self._list(s, list_name)
            ids = [self._contact(s, e).id for e in emails]
            return self._attach(s, lst.id, ids)

    def remove_from_list(self, list_name: str, emails: Iterable[str]) -> int:
        with self._db.session() as s:
            lst = self._list(s, list_name)
            ids = [self._contact(s, e).id for e in emails]
            result = s.execute(
                delete(list_members).where(
                    list_members.c.list_id == lst.id, list_members.c.contact_id.in_(ids)
                )
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # ---- contacts --------------------------------------------------------------------------

    def add(
        self,
        email: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        attributes: dict[str, Any] | None = None,
        lists: Iterable[str] = (),
        source: str | None = None,
    ) -> tuple[Contact, bool]:
        """Create or update a contact. Returns ``(contact, created)``."""
        with self._db.session() as s:
            contact, created = self._upsert(s, email, first_name, last_name, attributes, source)
            for name in lists:
                self._attach(s, self._list(s, name, create=True).id, [contact.id])
            return contact, created

    def get(self, email: str) -> Contact:
        with self._db.session() as s:
            return self._contact(s, email)

    def find(self, email: str) -> Contact | None:
        try:
            normalized = normalize_email(email)
        except ValidationError:
            return None
        with self._db.session() as s:
            return s.scalar(select(Contact).where(Contact.email == normalized))

    def count(self, *, status: ContactStatus | None = None, list_name: str | None = None) -> int:
        with self._db.session() as s:
            stmt = select(func.count(Contact.id))
            if list_name is not None:
                lst = self._list(s, list_name)
                stmt = stmt.join(list_members, list_members.c.contact_id == Contact.id).where(
                    list_members.c.list_id == lst.id
                )
            if status is not None:
                stmt = stmt.where(Contact.status == status)
            return int(s.scalar(stmt) or 0)

    def unsubscribe(
        self, email: str, *, reason: str = "unsubscribed", campaign_id: int | None = None
    ) -> bool:
        """Suppress a contact for good. Returns False if it was already not active."""
        return self._suppress(email, ContactStatus.UNSUBSCRIBED, reason, campaign_id)

    def mark_bounced(self, email: str, *, reason: str, campaign_id: int | None = None) -> bool:
        return self._suppress(email, ContactStatus.BOUNCED, reason, campaign_id)

    def mark_complained(self, email: str, *, reason: str = "spam complaint") -> bool:
        return self._suppress(email, ContactStatus.COMPLAINED, reason, None)

    def resubscribe(self, email: str, *, reason: str) -> None:
        """Explicit opt-in again. Never call this for a bulk import."""
        if not reason.strip():
            raise ValidationError("A reason is required to re-activate a suppressed contact")
        with self._db.session() as s:
            contact = self._contact(s, email)
            contact.status = ContactStatus.ACTIVE
            contact.status_reason = f"resubscribed: {reason}"
            contact.status_changed_at = utcnow()
            contact.status_campaign_id = None

    # ---- import / export -------------------------------------------------------------------

    def import_csv(
        self,
        source: str | Path | TextIO,
        *,
        list_name: str | None = None,
        encoding: str = "utf-8-sig",
        delimiter: str | None = None,
        source_label: str | None = None,
    ) -> ImportReport:
        """Import contacts from CSV.

        The header row must contain an e-mail column (``email``/``почта``/...). ``first_name``/
        ``имя`` and ``last_name``/``фамилия`` are recognised; every other column becomes a custom
        attribute usable in templates as ``{{ column_name }}``. The delimiter (``,`` ``;`` or tab)
        is auto-detected, which covers Excel exports in Russian locale.
        """
        if isinstance(source, str | Path):
            path = Path(source)
            try:
                content = path.read_text(encoding=encoding)
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    f"Cannot decode {path.name} as {encoding}; try encoding='cp1251'"
                ) from exc
            source_label = source_label or path.name
        else:
            content = source.read()
        return self._import_text(content, list_name, delimiter, source_label)

    def export_csv(self, dest: TextIO, *, list_name: str | None = None) -> int:
        with self._db.session() as s:
            stmt = select(Contact).order_by(Contact.id)
            if list_name is not None:
                lst = self._list(s, list_name)
                stmt = stmt.join(list_members, list_members.c.contact_id == Contact.id).where(
                    list_members.c.list_id == lst.id
                )
            contacts = s.scalars(stmt).all()
            writer = csv.writer(dest)
            writer.writerow(["email", "first_name", "last_name", "status", "status_reason"])
            for c in contacts:
                writer.writerow(
                    [
                        c.email,
                        c.first_name or "",
                        c.last_name or "",
                        c.status.value,
                        c.status_reason or "",
                    ]
                )
            return len(contacts)

    # ---- internals -------------------------------------------------------------------------

    def _import_text(
        self, content: str, list_name: str | None, delimiter: str | None, source_label: str | None
    ) -> ImportReport:
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(content[:4096], delimiters=",;\t").delimiter
            except csv.Error:
                delimiter = ","
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        if not reader.fieldnames:
            raise ValidationError("CSV file is empty")
        headers = {name: name.strip().lower() for name in reader.fieldnames if name}
        email_col = next((o for o, n in headers.items() if n in _EMAIL_HEADERS), None)
        if email_col is None:
            raise ValidationError(
                f"No e-mail column found. Header is {list(reader.fieldnames)}; "
                f"expected one of {sorted(_EMAIL_HEADERS)}"
            )
        first_col = next((o for o, n in headers.items() if n in _FIRST_NAME_HEADERS), None)
        last_col = next((o for o, n in headers.items() if n in _LAST_NAME_HEADERS), None)
        attr_cols = {o: n for o, n in headers.items() if o not in (email_col, first_col, last_col)}

        report = ImportReport()
        seen: set[str] = set()
        with self._db.session() as s:
            list_id = self._list(s, list_name, create=True).id if list_name else None
            member_ids: list[int] = []
            for row in reader:
                line = reader.line_num
                try:
                    email = normalize_email(row.get(email_col) or "")
                except ValidationError as exc:
                    report.invalid.append((line, str(exc)))
                    continue
                if email in seen:
                    report.duplicates_in_file += 1
                    continue
                seen.add(email)
                attributes = {
                    n: v.strip() for o, n in attr_cols.items() if (v := row.get(o)) and v.strip()
                }
                contact, created = self._upsert(
                    s,
                    email,
                    _clean(row.get(first_col) if first_col else None),
                    _clean(row.get(last_col) if last_col else None),
                    attributes,
                    source_label,
                )
                member_ids.append(contact.id)
                if contact.status is not ContactStatus.ACTIVE:
                    report.already_suppressed += 1
                elif created:
                    report.added += 1
                else:
                    report.updated += 1
            if list_id is not None:
                self._attach(s, list_id, member_ids)
        return report

    def _upsert(
        self,
        s: Session,
        raw_email: str,
        first_name: str | None,
        last_name: str | None,
        attributes: dict[str, Any] | None,
        source: str | None,
    ) -> tuple[Contact, bool]:
        email = normalize_email(raw_email)
        contact = s.scalar(select(Contact).where(Contact.email == email))
        if contact is None:
            contact = Contact(
                email=email,
                first_name=first_name,
                last_name=last_name,
                attributes=dict(attributes or {}),
                source=source,
            )
            s.add(contact)
            s.flush()
            return contact, True
        # Fill/refresh data but never touch status: suppression survives re-imports.
        if first_name:
            contact.first_name = first_name
        if last_name:
            contact.last_name = last_name
        if attributes:
            contact.attributes = {**contact.attributes, **attributes}
        return contact, False

    def _attach(self, s: Session, list_id: int, contact_ids: list[int]) -> int:
        if not contact_ids:
            return 0
        existing = set(
            s.scalars(
                select(list_members.c.contact_id).where(
                    list_members.c.list_id == list_id, list_members.c.contact_id.in_(contact_ids)
                )
            )
        )
        new = [cid for cid in dict.fromkeys(contact_ids) if cid not in existing]
        if new:
            now = utcnow()
            s.execute(
                list_members.insert(),
                [{"list_id": list_id, "contact_id": cid, "added_at": now} for cid in new],
            )
        return len(new)

    def _suppress(
        self, email: str, status: ContactStatus, reason: str, campaign_id: int | None
    ) -> bool:
        with self._db.session() as s:
            contact = s.scalar(select(Contact).where(Contact.email == normalize_email(email)))
            if contact is None or contact.status is not ContactStatus.ACTIVE:
                return False
            contact.status = status
            contact.status_reason = reason
            contact.status_changed_at = utcnow()
            contact.status_campaign_id = campaign_id
            return True

    def _list(self, s: Session, name: str, *, create: bool = False) -> MailingList:
        lst = s.scalar(select(MailingList).where(MailingList.name == name.strip()))
        if lst is None:
            if not create:
                raise NotFoundError(f"List {name!r} not found")
            lst = MailingList(name=name.strip())
            s.add(lst)
            s.flush()
        return lst

    def _contact(self, s: Session, email: str) -> Contact:
        contact = s.scalar(select(Contact).where(Contact.email == normalize_email(email)))
        if contact is None:
            raise NotFoundError(f"Contact {email!r} not found")
        return contact
