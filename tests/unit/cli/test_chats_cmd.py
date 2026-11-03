"""Tests for the `inhere-box chats` group."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from inhere_box.cli.main import cli


def test_chats_group_help_lists_all_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["chats", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "get", "create", "update", "delete", "send", "repl"):
        assert sub in result.output


def test_chats_send_help_lists_mode() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["chats", "send", "--help"])
    assert result.exit_code == 0
    assert "--mode" in result.output


def test_chats_list_requires_user_not_expert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INHERE_BOX_HOME", str(tmp_path))
    monkeypatch.delenv("INHERE_BOX_AGENT", raising=False)
    result = CliRunner().invoke(cli, ["chats", "list"])
    assert result.exit_code == 1
    assert "user" in result.output


def test_delete_thread_offline_unbinds_session(tmp_inhere_box_home: Path) -> None:
    from inhere_box.cli.support.db import open_cli_services
    from inhere_box.cli.support.offline_ops import (
        create_thread_offline,
        delete_thread_offline,
    )
    from inhere_box.infra.db.repos.agents import AgentRepo
    from inhere_box.infra.db.repos.users import UserRepo

    assert (
        CliRunner()
        .invoke(
            cli,
            ["init", "--admin-username", "alice", "--admin-password", "TestPass12", "--yes"],
        )
        .exit_code
        == 0
    )
    with open_cli_services(home=tmp_inhere_box_home) as svc:
        uid = UserRepo(svc.db).get_by_username("alice")
        assert uid is not None
        user_id = int(uid.id)
        AgentRepo(svc.db).create(agent_id="ag1", user_id=user_id, name="main")

    created = create_thread_offline(agent_id="ag1", user_id=user_id, home=tmp_inhere_box_home)
    delete_thread_offline("ag1", created["thread_id"], home=tmp_inhere_box_home)

    with open_cli_services(home=tmp_inhere_box_home) as svc:
        assert svc.thread_repo.get(created["thread_id"]) is None
        assert svc.session_repo.get(created["session_key"]) is None
