"""The README promises that examples/quickstart.py works; keep that promise honest."""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

from mailbot.models import CampaignStatus

from .conftest import Env

ROOT = Path(__file__).resolve().parent.parent


def test_quickstart_flow_runs_end_to_end(env: Env, tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("quickstart", ROOT / "examples" / "quickstart.py")
    assert spec is not None and spec.loader is not None
    quickstart = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quickstart)

    csv = tmp_path / "contacts.csv"
    csv.write_text("email,имя\nanna@mail.ru,Анна\noleg@yandex.ru,\n", encoding="utf-8")
    campaign_id = quickstart.prepare_campaign(
        env.bot, password="app-pass", contacts_csv=csv, test_to="me@example.com"
    )

    assert env.bot.campaigns.get(campaign_id).status is CampaignStatus.SCHEDULED
    assert [m["Subject"].startswith("[TEST]") for _, m in env.transport.sent] == [True]

    # The example schedules "tomorrow" by the real clock; move the fake clock just past that.
    scheduled_at = env.bot.campaigns.get(campaign_id).scheduled_at
    assert scheduled_at is not None
    env.clock.now = scheduled_at + timedelta(minutes=1)
    env.run_to_completion()
    subjects = {r: m["Subject"] for r, m in env.transport.sent if r != "me@example.com"}
    assert subjects == {
        "anna@mail.ru": "Новости октября, Анна",
        "oleg@yandex.ru": "Новости октября, друг",
    }
