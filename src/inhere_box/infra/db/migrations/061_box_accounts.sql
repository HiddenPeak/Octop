-- One local owner, separately paired clients, and revocable short-lived sessions.
CREATE TABLE IF NOT EXISTS box_account (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    box_id TEXT NOT NULL,
    owner_user_id INTEGER REFERENCES users(id),
    auth_revision BIGINT NOT NULL DEFAULT 0,
    claimed_at BIGINT,
    trust_digest TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS box_paired_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    public_key_json TEXT NOT NULL,
    public_key_fingerprint TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
    created_at BIGINT NOT NULL,
    last_seen_at BIGINT,
    revoked_at BIGINT
);

CREATE TABLE IF NOT EXISTS box_pairing_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK (purpose IN ('claim', 'enroll')),
    secret_hash TEXT NOT NULL,
    expires_at BIGINT NOT NULL,
    created_at BIGINT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('issued', 'candidate_bound', 'awaiting_approval', 'consumed', 'cancelled')),
    issued_by_client_id TEXT REFERENCES box_paired_clients(client_id),
    issuance_request_id TEXT,
    request_id TEXT UNIQUE,
    request_digest TEXT,
    candidate_key_json TEXT,
    candidate_fingerprint TEXT,
    candidate_name TEXT,
    candidate_platform TEXT,
    client_id TEXT REFERENCES box_paired_clients(client_id),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (issued_by_client_id, issuance_request_id)
);

CREATE TABLE IF NOT EXISTS box_auth_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id TEXT NOT NULL UNIQUE,
    client_id TEXT REFERENCES box_paired_clients(client_id),
    grant_id TEXT REFERENCES box_pairing_grants(grant_id),
    action TEXT NOT NULL,
    params_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    public_key_json TEXT NOT NULL,
    expires_at BIGINT NOT NULL,
    consumed_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_box_challenges_expiry ON box_auth_challenges(expires_at);

CREATE TABLE IF NOT EXISTS box_auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL REFERENCES box_paired_clients(client_id),
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    auth_revision BIGINT NOT NULL,
    boot_id TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    revoked_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_box_sessions_client ON box_auth_sessions(client_id);
UPDATE _schema_version SET version = 61;
