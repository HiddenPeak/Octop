"""Task replay advances current-turn, summary and historical checkpoints independently."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from inhere_box.config import InhereBoxConfig
from inhere_box.infra.db.migrate import run_migrations
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.services import RepoBundle, SharedServices
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.projects import tasks
from inhere_box.infra.projects.conversation_import_models import (
    ConversationImport,
    ImportMessage,
    ImportTurn,
)
from inhere_box.infra.projects.conversation_imports import import_conversation
from inhere_box.infra.projects.cursors import cursor_codec
from inhere_box.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "task-stream.db")
    run_migrations(db)
    service = SharedServices(PathLayout(tmp_path), InhereBoxConfig(), RepoBundle.from_pool(db))
    service.secret_repo.get_or_create("jwt", lambda: b"task-stream-test-key")
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
    try:
        yield service
    finally:
        db.close()


def import_body(
    *answers: str, source_revision: int = 0, has_source_completion: bool = True
) -> ConversationImport:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    timestamps = [start + timedelta(minutes=index) for index in range(len(answers))]
    return ConversationImport(
        source="codex",
        external_session_id="source-session",
        title="Imported session",
        source_updated_at=start + timedelta(days=source_revision + 1),
        source_revision=str(source_revision),
        turns=[
            ImportTurn(
                external_turn_id=f"source-{index}",
                user_message=ImportMessage(text=f"Question {index}", timestamp=timestamp),
                assistant_message=ImportMessage(
                    text=answer, timestamp=timestamp if has_source_completion else None
                ),
                completed_at=timestamp if has_source_completion else None,
            )
            for index, (answer, timestamp) in enumerate(zip(answers, timestamps, strict=True))
        ],
    )


@pytest.mark.parametrize("has_source_completion", [True, False])
def test_append_sends_new_turn_without_resending_unchanged_anchor(services, has_source_completion):
    initial = import_conversation(
        services, 1, import_body("First", "Second", has_source_completion=has_source_completion)
    ).task
    task_id = initial.task_id
    baseline = tasks.detail(services, 1, task_id)
    assert initial.event_cursor == baseline.event_cursor
    empty = tasks.stream_updates(services, 1, task_id, baseline.event_cursor)
    assert empty.updates == [] and empty.cursor == baseline.event_cursor
    assert not empty.has_more
    obsolete = cursor_codec(services, 1, f"conversation:{task_id}:events").encode(
        "conversation", position=2, after="old-anchor-digest"
    )
    with pytest.raises(InhereBoxError) as error:
        tasks.stream_updates(services, 1, task_id, obsolete)
    assert error.value.code is ErrorCode.PROJECT_SYNC_RESET

    latest = import_conversation(
        services,
        1,
        import_body(
            "First",
            "Second",
            "Third",
            source_revision=1,
            has_source_completion=has_source_completion,
        ),
    ).task
    assert initial.history_revision == latest.history_revision == 0
    batch = tasks.stream_updates(services, 1, task_id, baseline.event_cursor, limit=1)
    assert len(batch.updates) == 1 and not batch.has_more
    turn = batch.updates[0]
    assert isinstance(turn, tasks.TaskUpdate)
    assert turn.turn.reply == "Third" and turn.historical is None


def test_configuration_change_sends_only_summary(services):
    initial = import_conversation(services, 1, import_body("First", "Second")).task
    task_id = initial.task_id
    baseline = tasks.detail(services, 1, task_id)
    with services.db.transaction() as conn:
        repo = services.project_task_repo
        row = repo.get_in_transaction(conn, task_id, 1)
        assert row is not None
        changed = repo.update_configuration(conn, replace(row, action_mode="plan"), 1)
        assert changed is not None
    batch = tasks.stream_updates(services, 1, task_id, baseline.event_cursor)
    assert len(batch.updates) == 1 and not batch.has_more
    update = batch.updates[0]
    assert isinstance(update, tasks.TaskSummaryUpdate)
    assert update.task.action_mode == "plan" and update.task.configuration_revision == 2
    assert update.history_revision == 0
    assert tasks.stream_updates(services, 1, task_id, update.cursor).updates == []


def test_reimport_replays_older_rows_and_anchor_across_partial_cursors(services):
    initial = import_conversation(services, 1, import_body("First", "Second", "Third")).task
    task_id = initial.task_id
    baseline = tasks.detail(services, 1, task_id, limit=1)
    updated = import_conversation(
        services,
        1,
        import_body("First edited", "Second edited", "Third edited", source_revision=1),
    ).task
    assert tasks.detail(services, 1, task_id).history_revision == 1

    first = tasks.stream_updates(services, 1, task_id, baseline.event_cursor, limit=1)
    assert first.has_more
    second = tasks.stream_updates(
        services, 1, task_id, first.cursor, limit=1, watermark=first.watermark
    )
    assert second.has_more, "The anchor changed even though the newest row position is unchanged"
    # This append happened after the original catch-up fence.
    import_conversation(
        services,
        1,
        import_body("First edited", "Second edited", "Third edited", "Fourth", source_revision=2),
    )
    third = tasks.stream_updates(
        services, 1, task_id, second.cursor, limit=1, watermark=first.watermark
    )
    assert not third.has_more
    replay = first.updates + second.updates + third.updates
    assert all(isinstance(update, tasks.TaskUpdate) for update in replay)
    assert [update.turn.turn_id for update in replay] == [turn.turn_id for turn in initial.turns]
    assert [update.turn.reply for update in replay] == [
        "First edited",
        "Second edited",
        "Third edited",
    ]
    assert [update.historical for update in replay] == [True, True, None]
    assert [update.history_revision for update in replay] == [0, 1, 1]
    assert all(update.task.list_revision == updated.list_revision for update in replay)

    later = tasks.stream_updates(services, 1, task_id, third.cursor)
    assert len(later.updates) == 1
    assert isinstance(later.updates[0], tasks.TaskUpdate)
    assert later.updates[0].turn.reply == "Fourth"
    history_page = tasks.detail(services, 1, task_id, page_cursor=baseline.next_page_cursor)
    assert history_page.history_revision == 1 and history_page.event_cursor is None
    assert [turn.reply for turn in history_page.turns] == [
        "First edited",
        "Second edited",
    ]
