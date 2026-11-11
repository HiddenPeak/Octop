ALTER TABLE project_task_turns ADD COLUMN history_revision BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_project_task_turns_history_revision
    ON project_task_turns(task_id, history_revision, id) WHERE history_revision > 0;
