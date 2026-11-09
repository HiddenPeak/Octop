CREATE TABLE IF NOT EXISTS project_task_deleted_requests (
    owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    PRIMARY KEY (owner_id, request_id)
);

INSERT INTO project_task_deleted_requests(owner_id, request_id)
SELECT owner_id, payload_json::jsonb->>'request_id'
FROM project_sync_changes
WHERE scope='tasks:all' AND payload_json::jsonb->>'deleted'='true'
  AND payload_json::jsonb->>'request_id' IS NOT NULL
ON CONFLICT(owner_id, request_id) DO NOTHING;
