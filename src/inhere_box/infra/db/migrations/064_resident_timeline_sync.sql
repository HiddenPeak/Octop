CREATE TABLE IF NOT EXISTS resident_timeline_changes (
    thread_id TEXT NOT NULL REFERENCES resident_conversations(thread_id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    PRIMARY KEY (thread_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_resident_timeline_changes_revision
    ON resident_timeline_changes(thread_id, revision);
UPDATE _schema_version SET version = 64 WHERE version < 64;
