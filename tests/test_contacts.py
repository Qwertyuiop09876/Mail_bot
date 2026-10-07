from __future__ import annotations

import io

import pytest

from mailbot.contacts import ContactService
from mailbot.errors import NotFoundError, StateError, ValidationError
from mailbot.models import ContactStatus

from .conftest import Env


@pytest.fixture
def contacts(env: Env) -> ContactService:
    return env.bot.contacts


def test_emails_are_normalised_and_deduplicated(contacts: ContactService) -> None:
    a, created_a = contacts.add("  Anna@Mail.RU ")
    b, created_b = contacts.add("anna@mail.ru", first_name="Анна")
    assert (created_a, created_b) == (True, False)
    assert a.id == b.id and b.email == "anna@mail.ru" and b.first_name == "Анна"


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@", "a b@c.ru", "имя@почта.рф"])
def test_invalid_addresses_are_rejected(contacts: ContactService, bad: str) -> None:
    with pytest.raises(ValidationError):
        contacts.add(bad)


def test_csv_import_handles_excel_style_files(contacts: ContactService) -> None:
    # Semicolon delimiter, Russian headers, BOM-less text, one bad row, one duplicate.
    data = (
        "Почта;Имя;Фамилия;Город\n"
        "anna@mail.ru;Анна;Иванова;Москва\n"
        "oleg@yandex.ru;Олег;;Тверь\n"
        "broken-address;Игорь;;Омск\n"
        "ANNA@mail.ru;Анна;;Москва\n"
    )
    report = contacts.import_csv(io.StringIO(data), list_name="clients")
    assert (report.added, report.duplicates_in_file) == (2, 1)
    assert [line for line, _ in report.invalid] == [4]
    anna = contacts.get("anna@mail.ru")
    assert (anna.first_name, anna.last_name, anna.attributes) == (
        "Анна",
        "Иванова",
        {"город": "Москва"},
    )
    assert contacts.count(list_name="clients") == 2


def test_csv_without_email_column_is_refused(contacts: ContactService) -> None:
    with pytest.raises(ValidationError, match="No e-mail column"):
        contacts.import_csv(io.StringIO("name,phone\nAnna,123\n"))


def test_csv_file_with_bom_and_wrong_encoding(contacts: ContactService, tmp_path) -> None:  # type: ignore[no-untyped-def]
    good = tmp_path / "good.csv"
    good.write_bytes("﻿email,name\nanna@mail.ru,Анна\n".encode())
    assert contacts.import_csv(good).added == 1

    legacy = tmp_path / "legacy.csv"
    legacy.write_bytes("email,name\nolga@mail.ru,Ольга\n".encode("cp1251"))
    with pytest.raises(ValidationError, match="cp1251"):
        contacts.import_csv(legacy)
    assert contacts.import_csv(legacy, encoding="cp1251").added == 1


def test_reimport_never_resurrects_suppressed_contacts(contacts: ContactService) -> None:
    contacts.add("anna@mail.ru", lists=["clients"])
    assert contacts.unsubscribe("anna@mail.ru", reason="clicked link") is True
    assert contacts.unsubscribe("anna@mail.ru") is False  # already suppressed

    report = contacts.import_csv(
        io.StringIO("email,name\nanna@mail.ru,Анна\n"), list_name="clients"
    )
    assert report.already_suppressed == 1 and report.added == 0
    anna = contacts.get("anna@mail.ru")
    assert anna.status is ContactStatus.UNSUBSCRIBED and anna.first_name == "Анна"
    assert contacts.add("anna@mail.ru")[0].status is ContactStatus.UNSUBSCRIBED


def test_explicit_resubscribe_requires_a_reason(contacts: ContactService) -> None:
    contacts.add("anna@mail.ru")
    contacts.unsubscribe("anna@mail.ru")
    with pytest.raises(ValidationError):
        contacts.resubscribe("anna@mail.ru", reason=" ")
    contacts.resubscribe("anna@mail.ru", reason="asked by phone")
    assert contacts.get("anna@mail.ru").status is ContactStatus.ACTIVE


def test_list_counts_membership_and_removal(contacts: ContactService) -> None:
    for i in range(3):
        contacts.add(f"u{i}@x.ru", lists=["a"])
    contacts.add("u0@x.ru", lists=["b"])
    contacts.unsubscribe("u1@x.ru")
    info = {i.name: i for i in contacts.lists()}
    assert (info["a"].total, info["a"].active, info["b"].total) == (3, 2, 1)
    assert contacts.remove_from_list("a", ["u0@x.ru", "u2@x.ru"]) == 2
    assert contacts.add_to_list("a", ["u0@x.ru"]) == 1
    assert contacts.add_to_list("a", ["u0@x.ru"]) == 0  # idempotent
    with pytest.raises(NotFoundError):
        contacts.add_to_list("a", ["ghost@x.ru"])


def test_list_in_use_by_unfinished_campaign_cannot_be_deleted(env: Env) -> None:
    env.seed(2)
    with pytest.raises(StateError, match="unfinished"):
        env.bot.contacts.delete_list("clients")
    env.bot.contacts.create_list("spare")
    env.bot.contacts.delete_list("spare")


def test_export_roundtrip(contacts: ContactService) -> None:
    contacts.add("anna@mail.ru", first_name="Анна", lists=["a"])
    out = io.StringIO()
    assert contacts.export_csv(out, list_name="a") == 1
    assert out.getvalue().splitlines()[1] == "anna@mail.ru,Анна,,active,"
