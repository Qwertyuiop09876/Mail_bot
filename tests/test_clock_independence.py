"""Scheduling logic must run on the injected clock only.

This once passed by luck: the fake clock happened to be *later* than the real date, so code that
quietly used the real clock still looked right. Running with the fake clock far in the past and far
in the future makes any such leak fail loudly, whatever day the tests run on.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from mailbot.models import CampaignStatus, ContactStatus

from .conftest import Env


@pytest.mark.parametrize(
    "start", [datetime(2001, 1, 1, tzinfo=UTC), datetime(2099, 1, 1, tzinfo=UTC)]
)
def test_full_lifecycle_uses_only_the_injected_clock(
    make_env: Callable[..., Env], start: datetime
) -> None:
    env = make_env(start=start)
    cid = env.seed(3, daily_limit=2)

    env.bot.campaigns.send_now(cid)  # "now" must mean the fake now
    env.run_to_completion()
    assert len(env.transport.sent) == 2  # daily limit is counted in fake time too

    env.bot.contacts.unsubscribe("user2@test.org")
    assert env.bot.contacts.get("user2@test.org").status_changed_at == env.clock.now

    env.clock.advance(hours=25)
    env.run_to_completion()
    done = env.bot.campaigns.get(cid)
    assert done.status is CampaignStatus.COMPLETED
    assert done.finished_at is not None and done.finished_at.year == start.year
    assert env.bot.contacts.get("user2@test.org").status is ContactStatus.UNSUBSCRIBED
    assert len(env.transport.sent) == 2

    # cancel / retry also stamp fake time and make deliveries immediately due in fake time
    second = env.bot.campaigns.create(
        "Second",
        account="main",
        lists=["clients"],
        subject="S",
        html="<p>{{ unsubscribe_url }}</p>",
    ).id
    env.bot.accounts.set_limits("main", daily_limit=1)  # keeps the campaign unfinished
    env.bot.campaigns.send_now(second)
    env.bot.dispatcher.tick()
    assert env.bot.campaigns.get(second).status is CampaignStatus.SENDING
    env.bot.campaigns.cancel(second)
    cancelled = env.bot.campaigns.get(second)
    assert cancelled.finished_at is not None and cancelled.finished_at.year == start.year
