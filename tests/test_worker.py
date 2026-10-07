from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from mailbot.errors import ValidationError
from mailbot.models import CampaignStatus

from .conftest import Env


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_worker_loop_sends_then_stops_gracefully(make_env) -> None:  # type: ignore[no-untyped-def]
    env: Env = make_env(poll_interval_seconds=0.01)
    cid = env.seed(3)
    stop = threading.Event()
    worker = threading.Thread(target=env.bot.run_forever, args=(stop,), daemon=True)
    worker.start()
    try:
        env.bot.campaigns.send_now(cid)  # scheduled while the worker is already running
        assert wait_for(lambda: env.bot.campaigns.get(cid).status is CampaignStatus.COMPLETED)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(env.transport.sent) == 3


def test_worker_survives_a_failing_tick(make_env, caplog) -> None:  # type: ignore[no-untyped-def]
    env: Env = make_env(poll_interval_seconds=0.01)
    cid = env.seed(1)
    original = env.bot.dispatcher.tick
    calls = {"n": 0}

    def flaky_tick():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return original()

    env.bot.dispatcher.tick = flaky_tick  # type: ignore[method-assign]
    env.bot.campaigns.send_now(cid)
    stop = threading.Event()
    worker = threading.Thread(target=env.bot.run_forever, args=(stop,), daemon=True)
    worker.start()
    try:
        assert wait_for(lambda: env.bot.campaigns.get(cid).status is CampaignStatus.COMPLETED)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert "tick failed" in caplog.text


def test_stop_request_interrupts_rate_limit_sleep(make_env) -> None:  # type: ignore[no-untyped-def]
    # Real sleeping (no stub) at 1 message/minute: stop must not wait a minute.
    from mailbot.dispatcher import Dispatcher

    env: Env = make_env(poll_interval_seconds=0.01)
    cid = env.seed(5, rate_per_minute=1)
    # A second dispatcher on the same database, but with the real (interruptible) sleep.
    dispatcher = Dispatcher(
        env.bot.db,
        env.bot.settings,
        env.bot.accounts,
        env.bot.signer,
        transport_factory=lambda _c: env.transport,
        clock=env.clock,
    )
    env.bot.campaigns.send_now(cid)
    stop = threading.Event()
    worker = threading.Thread(target=dispatcher.run_forever, args=(stop,), daemon=True)
    started = time.monotonic()
    worker.start()
    assert wait_for(lambda: len(env.transport.sent) >= 1)
    stop.set()
    worker.join(timeout=5)
    assert not worker.is_alive() and time.monotonic() - started < 5
    assert len(env.transport.sent) <= 2


def test_account_and_template_crud(env: Env) -> None:
    env.seed(1)
    env.bot.accounts.set_password("main", "new-secret")
    acc = env.bot.accounts.get("main")
    assert env.bot.accounts.smtp_config(acc).password == "new-secret"
    updated = env.bot.accounts.set_limits("main", daily_limit=10, rate_per_minute=5)
    assert (updated.daily_limit, updated.rate_per_minute) == (10, 5)
    with pytest.raises(ValidationError):
        env.bot.accounts.set_limits("main", daily_limit=0)
    assert [a.name for a in env.bot.accounts.all()] == ["main"]

    tpl = env.bot.templates.save("t", subject="S", html="<p>x</p>")
    again = env.bot.templates.save("t", subject="S2", html="<p>y</p>", text="y")
    assert again.id == tpl.id and env.bot.templates.get("t").subject == "S2"
    assert [t.name for t in env.bot.templates.all()] == ["t"]
    with pytest.raises(ValidationError):
        env.bot.templates.save("", subject="S", html="<p>x</p>")
    env.bot.templates.delete("t")
    assert env.bot.templates.all() == []
