from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mailbot.errors import NotFoundError, StateError, ValidationError
from mailbot.models import CampaignStatus

from .conftest import Env


def test_campaign_copies_template_content(env: Env) -> None:
    env.seed(1)
    env.bot.templates.save(
        "welcome", subject="Привет, {{ first_name }}", html="<p>v1 {{ unsubscribe_url }}</p>"
    )
    c = env.bot.campaigns.create("C", account="main", lists=["clients"], template="welcome")
    env.bot.templates.save("welcome", subject="CHANGED", html="<p>v2</p>")
    again = env.bot.campaigns.get(c.id)
    assert again.subject == "Привет, {{ first_name }}" and "v1" in again.html


def test_explicit_content_overrides_template_and_something_is_required(env: Env) -> None:
    env.seed(1)
    env.bot.templates.save("t", subject="S", html="<p>H</p>")
    c = env.bot.campaigns.create(
        "C", account="main", lists=["clients"], template="t", subject="Own"
    )
    assert (c.subject, c.html) == ("Own", "<p>H</p>")
    with pytest.raises(ValidationError, match="Subject is required"):
        env.bot.campaigns.create("C2", account="main", lists=["clients"], html="<p>x</p>")


def test_unknown_references_are_reported(env: Env) -> None:
    env.seed(1)
    with pytest.raises(NotFoundError, match="Account"):
        env.bot.campaigns.create("C", account="nope", lists=["clients"], subject="s", html="h")
    with pytest.raises(NotFoundError, match="List"):
        env.bot.campaigns.create("C", account="main", lists=["nope"], subject="s", html="h")
    with pytest.raises(NotFoundError, match="Template"):
        env.bot.campaigns.create("C", account="main", lists=["clients"], template="nope")


def test_template_syntax_is_checked_on_save(env: Env) -> None:
    with pytest.raises(ValidationError, match="syntax"):
        env.bot.templates.save("bad", subject="s", html="{{ unclosed")


def test_validate_reports_problems_and_warnings(env: Env) -> None:
    cid = env.seed(3, html="<p>без ссылки отписки</p>", daily_limit=2)
    report = env.bot.campaigns.validate(cid)
    assert report.ok and report.recipients == 3
    joined = " ".join(report.warnings)
    assert "unsubscribe_url" in joined and "daily limit" in joined and "plain-text" in joined


def test_schedule_requires_recipients(env: Env) -> None:
    env.seed(1)
    env.bot.contacts.create_list("empty")
    cid = env.bot.campaigns.create(
        "C", account="main", lists=["empty"], subject="s", html="<p>x</p>"
    ).id
    with pytest.raises(ValidationError, match="No active recipients"):
        env.bot.campaigns.schedule(cid)


def test_schedule_requires_unsubscribe_url(make_env) -> None:  # type: ignore[no-untyped-def]
    env: Env = make_env(unsubscribe_base_url=None)
    cid = env.seed(1)
    with pytest.raises(ValidationError, match="UNSUBSCRIBE_BASE_URL"):
        env.bot.campaigns.schedule(cid)


def test_naive_datetimes_use_the_configured_timezone(env: Env) -> None:
    cid = env.seed(1)
    c = env.bot.campaigns.schedule(cid, datetime(2026, 10, 10, 12, 0))  # Europe/Moscow = UTC+3
    assert c.scheduled_at == datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def test_state_machine_rejects_illegal_transitions(env: Env) -> None:
    cid = env.seed(1)
    svc = env.bot.campaigns
    with pytest.raises(StateError):
        svc.pause(cid)  # draft can't be paused
    with pytest.raises(StateError):
        svc.cancel(cid)
    svc.schedule(cid, env.clock.now + timedelta(days=1))
    with pytest.raises(StateError):
        svc.update_content(cid, subject="late edit")  # frozen once scheduled
    with pytest.raises(StateError):
        svc.delete(cid)
    assert svc.unschedule(cid).status is CampaignStatus.DRAFT
    svc.update_content(cid, subject="ok now")
    svc.delete(cid)
    with pytest.raises(NotFoundError):
        svc.get(cid)


def test_preview_uses_a_real_recipient(env: Env) -> None:
    cid = env.seed(2)
    assert env.bot.campaigns.preview(cid).subject == "Новости для Имя0"
    assert env.bot.campaigns.preview(cid, email="user1@test.org").subject == "Новости для Имя1"


def test_account_cannot_be_deleted_while_used(env: Env) -> None:
    env.seed(1)
    with pytest.raises(StateError):
        env.bot.accounts.delete("main")


def test_password_is_stored_encrypted(env: Env) -> None:
    env.seed(1)
    acc = env.bot.accounts.get("main")
    assert "secret" not in acc.password_enc
    assert env.bot.accounts.smtp_config(acc).password == "secret"


def test_provider_presets(env: Env) -> None:
    acc = env.bot.accounts.add(
        "ya", provider="yandex360", from_email="Team@Example.com", password="p"
    )
    assert (acc.smtp_host, acc.smtp_port, acc.smtp_security.value) == ("smtp.yandex.ru", 465, "ssl")
    assert (acc.username, acc.imap_host, acc.daily_limit) == (
        "team@example.com",
        "imap.yandex.ru",
        3000,
    )
    personal = env.bot.accounts.add(
        "me", provider="yandex", from_email="me@yandex.ru", password="p"
    )
    assert (personal.daily_limit, personal.rate_per_minute) == (300, 10)  # conservative default
    with pytest.raises(ValidationError, match="smtp_host"):
        env.bot.accounts.add("x", provider="custom", from_email="a@b.ru", password="p")
    with pytest.raises(ValidationError, match="clear text"):
        env.bot.accounts.add(
            "plain", provider="custom", smtp_host="smtp.example.com", smtp_security="none",
            from_email="a@b.ru", password="p",
        )  # fmt: skip
    with pytest.raises(ValidationError, match="Unknown provider"):
        env.bot.accounts.add("y", provider="gmail", from_email="a@b.ru", password="p")


def test_yandex_login_must_equal_the_sender_but_a_bare_login_is_fine(env: Env) -> None:
    accounts = env.bot.accounts
    # login defaults to the full address
    accounts.add("full", provider="yandex", password="p", from_email="ivan@yandex.ru")
    accounts.add(
        "bare", provider="yandex", password="p", from_email="ivan@yandex.ru", username="ivan"
    )
    accounts.add(
        "other",
        provider="yandex",
        password="p",
        from_email="shop@yandex.ru",
        username="ivan@yandex.ru",
    )
    assert accounts.check_connection("full").warnings == []
    assert accounts.check_connection("bare").warnings == []  # "ivan" means ivan@yandex.ru
    assert "match the login exactly" in accounts.check_connection("other").warnings[0]


def test_password_change_is_verified_before_it_is_saved(env: Env) -> None:
    from mailbot.errors import AccountError

    env.seed(1)
    accounts = env.bot.accounts
    env.transport.connect_error = AccountError("535 5.7.8 Invalid user or password!", code=535)
    with pytest.raises(ValidationError, match=r"new password failed.*535"):
        accounts.set_password("main", "typo", verify=True)
    assert accounts.smtp_config(accounts.get("main")).password == "secret"  # unchanged

    env.transport.connect_error = None
    accounts.set_password("main", "fresh", verify=True)
    assert accounts.smtp_config(accounts.get("main")).password == "fresh"

    # check_connection can try a candidate without saving it
    assert accounts.check_connection("main", password="candidate").ok
    assert accounts.smtp_config(accounts.get("main")).password == "fresh"


def test_validate_warns_when_the_recipient_address_is_not_in_the_body(env: Env) -> None:
    no_email = env.seed(1)
    assert any("{{ email }}" in w for w in env.bot.campaigns.validate(no_email).warnings)
    with_email = env.bot.campaigns.create(
        "C2", account="main", lists=["clients"], subject="s",
        html="<p>Письмо для {{ email }}. <a href='{{ unsubscribe_url }}'>Отписаться</a></p>",
    ).id  # fmt: skip
    assert not any("{{ email }}" in w for w in env.bot.campaigns.validate(with_email).warnings)
