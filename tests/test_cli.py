from __future__ import annotations

import pytest
from click.testing import CliRunner

from mailbot.cli import cli
from mailbot.crypto import SecretBox, generate_key


def test_gen_key_prints_a_usable_key() -> None:
    result = CliRunner().invoke(cli, ["gen-key"])
    assert result.exit_code == 0
    SecretBox(result.output.strip())  # raises if invalid


@pytest.fixture
def cli_env(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAILBOT_SECRET_KEY", generate_key())
    monkeypatch.setenv("MAILBOT_DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    monkeypatch.setenv("MAILBOT_UNSUBSCRIBE_BASE_URL", "https://mail.example.com/unsubscribe")


def test_init_db_status_and_run_once(cli_env) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    assert runner.invoke(cli, ["init-db"]).exit_code == 0
    assert "Кампаний нет" in runner.invoke(cli, ["status"]).output
    result = runner.invoke(cli, ["run", "--once"])
    assert result.exit_code == 0 and "отправлено=0" in result.output


def test_missing_secret_key_is_a_clear_error(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from mailbot.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAILBOT_SECRET_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["mailbot", "init-db"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
