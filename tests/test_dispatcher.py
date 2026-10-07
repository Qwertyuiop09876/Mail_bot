from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from mailbot.errors import (
    AccountError,
    MessageRejected,
    RecipientRejected,
    TransientDeliveryError,
    ValidationError,
)
from mailbot.models import (
    Campaign,
    CampaignStatus,
    Contact,
    ContactStatus,
    Delivery,
    DeliveryStatus,
)

from .conftest import Env


def deliveries(env: Env, campaign_id: int) -> list[Delivery]:
    with env.bot.db.session() as s:
        return list(
            s.scalars(
                select(Delivery).where(Delivery.campaign_id == campaign_id).order_by(Delivery.id)
            )
        )


def campaign(env: Env, campaign_id: int) -> Campaign:
    return env.bot.campaigns.get(campaign_id)


def test_scheduled_campaign_waits_for_its_time(env: Env) -> None:
    cid = env.seed(3)
    env.bot.campaigns.schedule(cid, env.clock.now + timedelta(hours=2))

    env.bot.dispatcher.tick()
    assert env.transport.sent == []
    assert campaign(env, cid).status is CampaignStatus.SCHEDULED

    env.clock.advance(hours=2, minutes=1)
    env.run_to_completion()
    assert sorted(env.transport.recipients) == [f"user{i}@test.org" for i in range(3)]
    done = campaign(env, cid)
    assert done.status is CampaignStatus.COMPLETED
    assert done.finished_at is not None


def test_each_recipient_gets_exactly_one_personalised_message(env: Env) -> None:
    cid = env.seed(3)
    env.bot.campaigns.send_now(cid)
    env.run_to_completion()
    env.run_to_completion()  # extra ticks must not duplicate anything

    assert len(env.transport.sent) == 3
    to_user1 = next(m for r, m in env.transport.sent if r == "user1@test.org")
    assert to_user1["Subject"] == "Новости для Имя1"
    assert "Привет, Имя1!" in to_user1.get_body(("html",)).get_content()  # type: ignore[union-attr]
    assert to_user1["List-Unsubscribe"].startswith("<https://mail.example.com/unsubscribe/")
    assert to_user1["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert all(d.status is DeliveryStatus.SENT and d.message_id for d in deliveries(env, cid))


def test_contact_in_several_lists_is_mailed_once(env: Env) -> None:
    cid = env.seed(2)
    env.bot.contacts.add("user0@test.org", lists=["vip"])
    env.bot.contacts.add("extra@test.org", lists=["vip"])
    campaign_id = env.bot.campaigns.create(
        "Both",
        account="main",
        lists=["clients", "vip"],
        subject="Hi",
        html="<p>x {{ unsubscribe_url }}</p>",
    ).id
    env.bot.campaigns.send_now(campaign_id)
    env.run_to_completion()
    assert sorted(env.transport.recipients) == [
        "extra@test.org",
        "user0@test.org",
        "user1@test.org",
    ]
    assert cid != campaign_id


def test_rate_limit_spaces_messages(make_env) -> None:  # type: ignore[no-untyped-def]
    env = make_env()
    cid = env.seed(4, rate_per_minute=30)  # one message per 2 seconds
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    assert len(env.transport.sent) == 4
    assert len(env.sleeps) == 3
    assert all(1.9 < s <= 2.0 for s in env.sleeps)


def test_daily_limit_is_a_rolling_24_hours(env: Env) -> None:
    cid = env.seed(5, daily_limit=3)
    env.bot.campaigns.send_now(cid)

    env.bot.dispatcher.tick()
    env.bot.dispatcher.tick()
    assert len(env.transport.sent) == 3
    assert campaign(env, cid).status is CampaignStatus.SENDING

    env.clock.advance(hours=23)
    env.bot.dispatcher.tick()
    assert len(env.transport.sent) == 3  # still inside the window

    env.clock.advance(hours=2)
    env.run_to_completion()
    assert len(env.transport.sent) == 5
    assert campaign(env, cid).status is CampaignStatus.COMPLETED


def test_temporary_error_is_retried_with_backoff(env: Env) -> None:
    cid = env.seed(2)
    failures = {"left": 1}

    def flaky(_m, recipient):  # type: ignore[no-untyped-def]
        if recipient == "user0@test.org" and failures["left"]:
            failures["left"] -= 1
            return TransientDeliveryError("451 4.3.0 try later", code=451)
        return None

    env.transport.behavior = flaky
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    assert env.transport.recipients == ["user1@test.org"]
    retried = next(d for d in deliveries(env, cid) if d.email == "user0@test.org")
    assert retried.status is DeliveryStatus.PENDING and retried.attempts == 1

    env.bot.dispatcher.tick()  # backoff (60s) has not elapsed yet
    assert env.transport.attempts.count("user0@test.org") == 1

    env.clock.advance(seconds=61)
    env.run_to_completion()
    assert sorted(env.transport.recipients) == ["user0@test.org", "user1@test.org"]


def test_gives_up_after_max_attempts(env: Env) -> None:
    cid = env.seed(1)
    env.transport.behavior = lambda _m, _r: TransientDeliveryError("450 mailbox busy", code=450)
    env.bot.campaigns.send_now(cid)
    for _ in range(5):
        env.bot.dispatcher.tick()
        env.clock.advance(hours=1)
    (d,) = deliveries(env, cid)
    assert d.status is DeliveryStatus.FAILED and d.attempts == 3
    assert "gave up" in (d.last_error or "")
    assert campaign(env, cid).status is CampaignStatus.COMPLETED


def test_hard_bounce_suppresses_contact_for_future_campaigns(env: Env) -> None:
    cid = env.seed(2)
    env.transport.behavior = lambda _m, r: (
        RecipientRejected("550 5.1.1 User unknown", code=550) if r == "user0@test.org" else None
    )
    env.bot.campaigns.send_now(cid)
    env.run_to_completion()

    contact = env.bot.contacts.get("user0@test.org")
    assert contact.status is ContactStatus.BOUNCED
    assert contact.status_campaign_id == cid

    second = env.bot.campaigns.create(
        "Next",
        account="main",
        lists=["clients"],
        subject="Again",
        html="<p>{{ unsubscribe_url }}</p>",
    )
    assert env.bot.campaigns.validate(second.id).recipients == 1


def test_account_error_pauses_campaign_without_losing_the_recipient(env: Env) -> None:
    cid = env.seed(3)
    env.transport.behavior = lambda _m, _r: AccountError("535 5.7.8 bad credentials", code=535)
    env.bot.campaigns.send_now(cid)
    report = env.bot.dispatcher.tick()

    paused = campaign(env, cid)
    assert paused.status is CampaignStatus.PAUSED
    assert "535" in (paused.pause_reason or "")
    assert report.paused and report.sent == 0
    assert all(d.status is DeliveryStatus.PENDING and d.attempts == 0 for d in deliveries(env, cid))

    env.transport.behavior = lambda _m, _r: None  # password fixed
    env.bot.campaigns.resume(cid)
    env.run_to_completion()
    assert len(env.transport.sent) == 3


def test_bad_credentials_at_connect_pause_without_burning_attempts(env: Env) -> None:
    cid = env.seed(2)
    env.transport.connect_error = AccountError("535 authentication failed", code=535)
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    assert campaign(env, cid).status is CampaignStatus.PAUSED
    assert env.transport.attempts == []


def test_server_down_backs_off_without_burning_attempts(env: Env) -> None:
    cid = env.seed(3)
    env.transport.connect_error = TransientDeliveryError("connection refused")
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    env.bot.dispatcher.tick()
    assert env.transport.connects == 1  # second tick respected the account back-off
    assert all(d.attempts == 0 for d in deliveries(env, cid))

    env.transport.connect_error = None
    env.clock.advance(seconds=61)
    env.run_to_completion()
    assert len(env.transport.sent) == 3


def test_repeated_temporary_errors_trip_the_circuit_breaker(env: Env) -> None:
    cid = env.seed(6)
    env.transport.behavior = lambda _m, _r: TransientDeliveryError("421 slow down", code=421)
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    assert len(env.transport.attempts) == 3  # stopped after the threshold, not after all six
    env.bot.dispatcher.tick()
    assert len(env.transport.attempts) == 3  # account is backing off


def test_repeated_content_rejections_pause_the_campaign(env: Env) -> None:
    cid = env.seed(6)
    env.transport.behavior = lambda _m, _r: MessageRejected(
        "554 5.7.1 Message rejected under suspicion of SPAM", code=554
    )
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    paused = campaign(env, cid)
    assert paused.status is CampaignStatus.PAUSED
    assert "rejected" in (paused.pause_reason or "")
    assert len(env.transport.attempts) == 3
    assert {d.status for d in deliveries(env, cid)} == {
        DeliveryStatus.FAILED,
        DeliveryStatus.PENDING,
    }


def test_unsubscribe_between_schedule_and_start_is_honoured(env: Env) -> None:
    cid = env.seed(3)
    env.bot.campaigns.schedule(cid, env.clock.now + timedelta(hours=1))
    env.bot.contacts.unsubscribe("user1@test.org")
    env.clock.advance(hours=1, seconds=1)
    env.run_to_completion()
    assert sorted(env.transport.recipients) == ["user0@test.org", "user2@test.org"]


def test_unsubscribe_during_sending_is_honoured_at_send_time(env: Env) -> None:
    cid = env.seed(3, daily_limit=1)
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()  # recipients are materialised, but the daily limit lets one through
    assert env.transport.recipients == ["user0@test.org"]

    env.bot.contacts.unsubscribe("user2@test.org")
    env.clock.advance(hours=25)
    env.run_to_completion()
    env.clock.advance(hours=25)
    env.run_to_completion()

    assert env.transport.recipients == ["user0@test.org", "user1@test.org"]
    (skipped,) = [d for d in deliveries(env, cid) if d.email == "user2@test.org"]
    assert skipped.status is DeliveryStatus.SKIPPED
    assert "unsubscribed" in (skipped.last_error or "")


def test_interrupted_delivery_is_never_resent_automatically(env: Env) -> None:
    cid = env.seed(2)
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    env.transport.sent.clear()
    # Simulate a crash: one delivery was claimed ("sending") but its outcome never recorded.
    with env.bot.db.session() as s:
        d = s.scalars(select(Delivery).where(Delivery.campaign_id == cid)).first()
        assert d is not None
        d.status = DeliveryStatus.SENDING
        d.sent_at = None
        d.updated_at = env.clock.now
        s.execute(update(Campaign).where(Campaign.id == cid).values(status=CampaignStatus.SENDING))
    env.clock.advance(seconds=env.bot.settings.stale_sending_seconds + 1)
    env.run_to_completion()

    assert env.transport.sent == []  # no duplicate
    stuck = [d for d in deliveries(env, cid) if d.status is DeliveryStatus.FAILED]
    assert len(stuck) == 1 and "state unknown" in (stuck[0].last_error or "")

    assert env.bot.campaigns.retry_failed(cid) == 1  # explicit operator decision
    env.run_to_completion()
    assert len(env.transport.sent) == 1


def test_cancel_skips_unsent_messages(env: Env) -> None:
    cid = env.seed(5, daily_limit=2)
    env.bot.campaigns.send_now(cid)
    env.bot.dispatcher.tick()
    assert len(env.transport.sent) == 2
    env.bot.campaigns.cancel(cid)
    env.clock.advance(hours=30)
    env.run_to_completion()
    assert len(env.transport.sent) == 2
    stats = env.bot.campaigns.stats(cid)
    assert (stats.sent, stats.skipped, stats.remaining) == (2, 3, 0)
    assert stats.status is CampaignStatus.CANCELLED


def test_pause_and_resume(env: Env) -> None:
    cid = env.seed(3)
    env.bot.campaigns.send_now(cid)
    env.bot.campaigns.pause(cid)
    env.bot.dispatcher.tick()
    assert env.transport.sent == []

    env.bot.campaigns.resume(cid)
    assert campaign(env, cid).status is CampaignStatus.SCHEDULED  # was paused before starting
    env.run_to_completion()
    assert len(env.transport.sent) == 3


def test_one_unrenderable_contact_does_not_stop_the_mailing(env: Env) -> None:
    cid = env.seed(3, html="<p>{{ city }} {{ unsubscribe_url }}</p>")
    # Nobody has a "city" yet: the campaign must refuse to be scheduled.
    with pytest.raises(ValidationError, match="cannot be rendered"):
        env.bot.campaigns.send_now(cid)

    for i in range(3):
        env.bot.contacts.add(f"user{i}@test.org", attributes={"city": "Москва"})
    env.bot.campaigns.send_now(cid)
    # Data changes after scheduling: user2 loses the attribute. Only that delivery may fail.
    with env.bot.db.session() as s:
        s.execute(update(Contact).where(Contact.email == "user2@test.org").values(attributes={}))
    env.run_to_completion()

    assert sorted(env.transport.recipients) == ["user0@test.org", "user1@test.org"]
    (failed,) = [d for d in deliveries(env, cid) if d.status is DeliveryStatus.FAILED]
    assert failed.email == "user2@test.org" and "render failed" in (failed.last_error or "")
    assert campaign(env, cid).status is CampaignStatus.COMPLETED


def test_send_test_prefixes_subject_and_skips_bookkeeping(env: Env) -> None:
    cid = env.seed(2)
    env.bot.dispatcher.send_test(cid, "Boss@Example.com")
    ((recipient, message),) = env.transport.sent
    assert recipient == "boss@example.com"
    assert message["Subject"].startswith("[TEST] ")
    assert deliveries(env, cid) == []
    with env.bot.db.session() as s:
        assert s.scalar(select(Contact).where(Contact.email == "boss@example.com")) is None
