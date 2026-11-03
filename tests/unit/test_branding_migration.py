"""Renaming an installation must keep its existing configuration and data."""

from __future__ import annotations

from pathlib import Path

import pytest

from inhere_box.config import DatabaseConfig, load_config
from inhere_box.environment import env_get
from inhere_box.infra.utils.paths import PathLayout


def test_fresh_install_uses_new_home(monkeypatch) -> None:
    monkeypatch.delenv("INHERE_BOX_HOME", raising=False)
    monkeypatch.delenv("OCTOP_HOME", raising=False)
    assert PathLayout.from_env().root == Path.home() / ".inhere-box"


def test_existing_install_retains_database_and_password_file(monkeypatch) -> None:
    monkeypatch.delenv("INHERE_BOX_HOME", raising=False)
    monkeypatch.delenv("OCTOP_HOME", raising=False)
    legacy = Path.home() / ".octop"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"port": 8123}')
    database = legacy / "octop.db"
    database.write_bytes(b"existing-database")
    password = legacy / "octop-login.txt"
    password.write_text("existing-password")

    paths = PathLayout.from_env()
    assert paths.root == legacy
    assert paths.db == database
    assert paths.wizard_password_file == password
    assert load_config(paths.config).port == 8123
    assert DatabaseConfig().resolve_sqlite_path(paths.root) == database
    assert database.read_bytes() == b"existing-database"
    assert not (Path.home() / ".inhere-box").exists()


def test_new_environment_overrides_legacy_without_copying(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "old"))
    monkeypatch.delenv("INHERE_BOX_HOME", raising=False)
    assert PathLayout.from_env().root == tmp_path / "old"
    monkeypatch.setenv("INHERE_BOX_HOME", str(tmp_path / "new"))
    assert PathLayout.from_env().root == tmp_path / "new"


def test_config_accepts_legacy_env_but_prefers_new(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OCTOP_PORT", "8123")
    monkeypatch.delenv("INHERE_BOX_PORT", raising=False)
    assert load_config(tmp_path / "config.json").port == 8123
    monkeypatch.setenv("INHERE_BOX_PORT", "8124")
    assert load_config(tmp_path / "config.json").port == 8124
    monkeypatch.setenv("INHERE_BOX_PORT", "")
    assert env_get("INHERE_BOX_PORT") == ""


def test_new_database_filename_takes_precedence(tmp_path) -> None:
    (tmp_path / "octop.db").write_bytes(b"legacy")
    current = tmp_path / "inhere-box.db"
    current.write_bytes(b"current")
    assert PathLayout(tmp_path).db == current
    assert DatabaseConfig().resolve_sqlite_path(tmp_path) == current


def test_legacy_backup_names_keep_their_original_timestamp(tmp_path) -> None:
    from inhere_box.infra.backup.store import is_auto_backup_filename, resolve_backup_created_at

    for prefix in ("octop", "inhere-box"):
        name = f"{prefix}-auto-backup-20261029T030000Z.tar.gz"
        path = tmp_path / name
        path.touch()
        assert is_auto_backup_filename(name)
        assert resolve_backup_created_at(name, path, mtime=0) == "2026-10-29T03:00:00+00:00"
        assert not is_auto_backup_filename(f"{prefix}-backup-20261029T030000Z.tar.gz")


def test_pre_rename_project_cursor_remains_valid(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from inhere_box.infra.projects import cursors

    monkeypatch.setattr(
        cursors,
        "get_or_create_identity",
        lambda path: SimpleNamespace(boxID="box", restoreEpoch=2),
    )
    services = SimpleNamespace(
        paths=PathLayout(tmp_path),
        secret_repo=SimpleNamespace(get=lambda key: b"original-installation-secret"),
    )
    codec = cursors.cursor_codec(services, 7, "projects")
    # Fixture produced by the pre-rename v2 cursor format for this identity.
    assert codec.decode("KszH5zKJV6fB", "sync").position == 42
    current = codec.encode("sync", position=42)
    assert current != "KszH5zKJV6fB"
    assert cursors.CursorCodec(codec.context, codec.key).decode(current, "sync").position == 42

    from inhere_box.infra.errors import InhereBoxError

    for owner, scope in [(8, "projects"), (7, "tasks")]:
        other = cursors.cursor_codec(services, owner, scope)
        for token in (current, "KszH5zKJV6fB"):
            with pytest.raises(InhereBoxError):
                other.decode(token, "sync")


def test_historical_plan_reads_with_new_source_without_mutating_original() -> None:
    from inhere_box.infra.projects.progress import TaskPlan

    original = {
        "source": "octop_planning",
        "plan_id": "existing-plan",
        "revision": 3,
        "steps": [{"step_id": "existing-step", "content": "Continue", "status": "pending"}],
    }
    plan = TaskPlan.model_validate(original)
    assert plan.model_dump()["source"] == "inhere_box_planning"
    assert plan.plan_id == "existing-plan" and plan.steps[0].step_id == "existing-step"
    assert original["source"] == "octop_planning"


def test_historical_engine_snapshot_uses_new_identifier() -> None:
    from inhere_box.infra.projects.engine_catalog import ExecutionEngineSnapshot

    original = {"engine_id": "octop", "actual_model_id": "provider/model", "resumed": True}
    snapshot = ExecutionEngineSnapshot.model_validate(original)
    assert snapshot.model_dump()["engine_id"] == "inhere-box"
    assert snapshot.actual_model_id == "provider/model" and snapshot.resumed
    assert original["engine_id"] == "octop"


def test_old_backup_manifest_reads_but_new_archive_writes_new_version_key() -> None:
    import json

    from inhere_box.infra.backup.manifest import BackupManifest

    manifest = BackupManifest.from_dict({"octop_version": "1.0.0"})
    assert manifest.inhere_box_version == "1.0.0"
    assert manifest.db_file == "db/octop.db"
    current = json.loads(manifest.to_json())
    assert current["inhere_box_version"] == "1.0.0" and "octop_version" not in current


@pytest.mark.parametrize("data", [{"value": "small"}, {"value": "x" * 5000}])
def test_legacy_plugin_envelope_is_emitted_under_new_key(data) -> None:
    import json

    from langchain_core.messages import ToolMessage

    from inhere_box.infra.agents.middleware.inhere_box_ui_offload import (
        InhereBoxUiOffloadMiddleware,
    )

    message = ToolMessage(
        content=json.dumps({"octop_ui": {"renderer": "existing-plugin"}, "data": data}),
        tool_call_id="call",
    )
    InhereBoxUiOffloadMiddleware()._offload(message)
    output = json.loads(message.content)
    assert output["inhere_box_ui"] == {"renderer": "existing-plugin"}
    assert "octop_ui" not in output
    assert output.get("data", message.artifact) == data


def test_legacy_channel_binding_read_preserves_new_key_precedence() -> None:
    from inhere_box.infra.utils.branding import metadata_value, pop_metadata

    config = {"_octop_task": {"project_id": "old"}, "_inhere_box_task": {"project_id": "new"}}
    assert metadata_value(config, "_inhere_box_task") == {"project_id": "new"}
    assert pop_metadata(config, "_inhere_box_task") == {"project_id": "new"}
    assert config == {}
    assert metadata_value({"octop_stream_error": True}, "inhere_box_stream_error") is True
