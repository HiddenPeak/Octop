-- Management credentials and guest identities are deliberately not backfilled.
-- Existing v61 authorization requires an explicit trusted local upgrade.
ALTER TABLE box_account ADD COLUMN auth_model_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE box_account ADD COLUMN primary_client_id TEXT REFERENCES box_paired_clients(client_id);
ALTER TABLE box_account ADD COLUMN management_key_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE box_account ADD COLUMN management_key_revision BIGINT NOT NULL DEFAULT 0;

ALTER TABLE box_paired_clients ADD COLUMN role TEXT NOT NULL DEFAULT 'legacy'
    CHECK (role IN ('legacy', 'primary', 'guest'));
ALTER TABLE box_paired_clients ADD COLUMN guest_user_id INTEGER REFERENCES users(id);
ALTER TABLE box_auth_sessions ADD COLUMN client_role TEXT NOT NULL DEFAULT 'legacy';
CREATE UNIQUE INDEX IF NOT EXISTS idx_box_active_primary ON box_paired_clients(role)
    WHERE role = 'primary' AND state = 'active';

CREATE TABLE IF NOT EXISTS box_admin_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    auth_revision BIGINT NOT NULL,
    management_key_revision BIGINT NOT NULL,
    boot_id TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    revoked_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_box_admin_sessions_expiry ON box_admin_sessions(expires_at);
UPDATE _schema_version SET version = 62;
