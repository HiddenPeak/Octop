CREATE TABLE IF NOT EXISTS project_task_open_receipts (
    task_id TEXT NOT NULL REFERENCES project_tasks(task_id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    open_id TEXT NOT NULL,
    opened_at BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, owner_id, open_id)
);
