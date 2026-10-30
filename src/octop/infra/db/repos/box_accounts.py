"""SQL for the local-owner authentication aggregate.

The caller supplies the transaction; all mutations lock the singleton first.
This gives PostgreSQL the same serialization as SQLite's BEGIN IMMEDIATE.
"""

from __future__ import annotations

from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import insert_returning_id


class BoxAccountRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def initialize(self, conn: Any, box_id: str) -> None:
        conn.execute(
            "INSERT INTO box_account(singleton,box_id) VALUES(1,?) "
            "ON CONFLICT(singleton) DO NOTHING",
            (box_id,),
        )

    def account(self, conn: Any, *, lock: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if lock and self._db.dialect == "postgresql" else ""
        row = conn.execute("SELECT * FROM box_account WHERE singleton=1" + suffix).fetchone()
        return dict(row) if row is not None else None

    def trust_clients(self, conn: Any, *, legacy: bool = False) -> list[dict[str, Any]]:
        extra = "" if legacy else ",role,guest_user_id"
        rows = conn.execute(
            "SELECT client_id,owner_user_id,public_key_json,public_key_fingerprint,"
            "state,created_at,revoked_at" + extra + " FROM box_paired_clients ORDER BY client_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def trust_principals(self, conn: Any) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id,role,permissions,disabled FROM users WHERE id IN "
            "(SELECT owner_user_id FROM box_account UNION SELECT guest_user_id FROM box_paired_clients) "
            "ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def set_auth_model(self, conn: Any, version: int) -> None:
        conn.execute("UPDATE box_account SET auth_model_version=? WHERE singleton=1", (version,))

    def set_management_key(self, conn: Any, secret_hash: str) -> None:
        conn.execute(
            "UPDATE box_account SET management_key_hash=?,management_key_revision=management_key_revision+1 "
            "WHERE singleton=1",
            (secret_hash,),
        )

    def revoke_admin_sessions(self, conn: Any, now: int) -> None:
        conn.execute("UPDATE box_admin_sessions SET revoked_at=? WHERE revoked_at IS NULL", (now,))

    def close_all_grants(self, conn: Any) -> None:
        conn.execute(
            "UPDATE box_pairing_grants SET state='cancelled' WHERE state NOT IN ('consumed','cancelled')"
        )

    def set_primary(self, conn: Any, client_id: str | None) -> None:
        conn.execute("UPDATE box_account SET primary_client_id=? WHERE singleton=1", (client_id,))

    def set_client_role(self, conn: Any, client_id: str, role: str, now: int) -> None:
        conn.execute("UPDATE box_paired_clients SET role=? WHERE client_id=?", (role, client_id))
        conn.execute(
            "UPDATE box_auth_sessions SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL",
            (now, client_id),
        )
        conn.execute(
            "UPDATE box_pairing_grants SET state='cancelled' WHERE issued_by_client_id=? "
            "AND state NOT IN ('consumed','cancelled')",
            (client_id,),
        )

    def save_trust(self, conn: Any, *, revision: int, digest: str) -> None:
        conn.execute(
            "UPDATE box_account SET auth_revision=?,trust_digest=? WHERE singleton=1",
            (revision, digest),
        )

    def owner_count(self, conn: Any) -> int:
        return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def create_owner(self, conn: Any, now: int) -> int:
        owner_id = insert_returning_id(
            conn,
            "INSERT INTO users(username,password_hash,role,display_name,locale,"
            "disabled,created_at,permissions) VALUES('owner',NULL,'admin',NULL,'zh',0,?,'[]')",
            (now,),
        )
        conn.execute(
            "UPDATE box_account SET owner_user_id=?,claimed_at=? WHERE singleton=1",
            (owner_id, now),
        )
        return owner_id

    def owner(self, conn: Any, owner_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM users WHERE id=?", (owner_id,)).fetchone()
        return dict(row) if row is not None else None

    def create_guest(self, conn: Any, client_id: str, name: str, now: int) -> int:
        return insert_returning_id(
            conn,
            "INSERT INTO users(username,password_hash,role,display_name,locale,disabled,created_at,permissions) "
            "VALUES(?,NULL,'user',?,'zh',0,?,'[]')",
            ("box_guest_" + client_id.lower(), name, now),
        )

    def client(self, conn: Any, client_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_paired_clients WHERE client_id=?", (client_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def client_by_key(self, conn: Any, fingerprint: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_paired_clients WHERE public_key_fingerprint=?", (fingerprint,)
        ).fetchone()
        return dict(row) if row is not None else None

    def clients(self, conn: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute("SELECT * FROM box_paired_clients ORDER BY id")]

    def create_client(
        self,
        conn: Any,
        *,
        client_id: str,
        owner_id: int,
        guest_user_id: int,
        role: str,
        candidate: dict[str, Any],
        now: int,
    ) -> None:
        conn.execute(
            "INSERT INTO box_paired_clients(client_id,owner_user_id,name,platform,public_key_json,"
            "public_key_fingerprint,state,created_at,guest_user_id,role) VALUES(?,?,?,?,?,?,'active',?,?,?)",
            (
                client_id,
                owner_id,
                candidate["candidate_name"],
                candidate["candidate_platform"],
                candidate["candidate_key_json"],
                candidate["candidate_fingerprint"],
                now,
                guest_user_id,
                role,
            ),
        )

    def revoke_client(self, conn: Any, client_id: str, now: int) -> None:
        conn.execute(
            "UPDATE box_paired_clients SET state='revoked',revoked_at=? WHERE client_id=?",
            (now, client_id),
        )
        conn.execute(
            "UPDATE box_auth_sessions SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL",
            (now, client_id),
        )
        conn.execute(
            "UPDATE box_pairing_grants SET state='cancelled' WHERE issued_by_client_id=? "
            "AND state NOT IN ('consumed','cancelled')",
            (client_id,),
        )

    def grant(self, conn: Any, grant_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_pairing_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def grant_by_secret_hash(self, conn: Any, secret_hash: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_pairing_grants WHERE secret_hash=?", (secret_hash,)
        ).fetchone()
        return dict(row) if row is not None else None

    def grant_by_request(self, conn: Any, request_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_pairing_grants WHERE request_id=?", (request_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def grant_by_issuance(
        self, conn: Any, client_id: str | None, request_id: str
    ) -> dict[str, Any] | None:
        if client_id is None:
            row = conn.execute(
                "SELECT * FROM box_pairing_grants WHERE issued_by_client_id IS NULL AND issuance_request_id=?",
                (request_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        row = conn.execute(
            "SELECT * FROM box_pairing_grants WHERE issued_by_client_id=? AND issuance_request_id=?",
            (client_id, request_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def create_grant(
        self,
        conn: Any,
        *,
        grant_id: str,
        purpose: str,
        secret_hash: str,
        expires_at: int,
        now: int,
        issued_by: str | None,
        request_id: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO box_pairing_grants(grant_id,purpose,secret_hash,expires_at,created_at,"
            "state,issued_by_client_id,issuance_request_id) VALUES(?,?,?,?,?,'issued',?,?)",
            (grant_id, purpose, secret_hash, expires_at, now, issued_by, request_id),
        )

    def bind_candidate(
        self,
        conn: Any,
        *,
        grant_id: str,
        request_id: str,
        request_digest: str,
        public_key_json: str,
        fingerprint: str,
        name: str,
        platform: str,
    ) -> None:
        conn.execute(
            "UPDATE box_pairing_grants SET state='candidate_bound',request_id=?,request_digest=?,"
            "candidate_key_json=?,candidate_fingerprint=?,candidate_name=?,candidate_platform=? "
            "WHERE grant_id=? AND state='issued'",
            (request_id, request_digest, public_key_json, fingerprint, name, platform, grant_id),
        )

    def grant_state(
        self, conn: Any, grant_id: str, state: str, *, client_id: str | None = None
    ) -> None:
        conn.execute(
            "UPDATE box_pairing_grants SET state=?,client_id=? WHERE grant_id=?",
            (state, client_id, grant_id),
        )

    def fail_grant(self, conn: Any, grant_id: str) -> None:
        conn.execute(
            "UPDATE box_pairing_grants SET failed_attempts=failed_attempts+1 WHERE grant_id=?",
            (grant_id,),
        )

    def close_claim_grants(self, conn: Any, *, except_id: str = "") -> None:
        conn.execute(
            "UPDATE box_pairing_grants SET state='cancelled' WHERE purpose='claim' "
            "AND state NOT IN ('consumed','cancelled') AND grant_id<>?",
            (except_id,),
        )

    def pending_grants(self, conn: Any, now: int) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM box_pairing_grants WHERE expires_at>? "
                "AND state NOT IN ('consumed','cancelled')",
                (now,),
            ).fetchone()["n"]
        )

    def awaiting_approval(self, conn: Any, client_id: str, now: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT grant_id AS pairing_id,candidate_name AS name,candidate_platform AS platform,"
            "candidate_fingerprint AS public_key_fingerprint,state,expires_at "
            "FROM box_pairing_grants WHERE issued_by_client_id=? AND purpose='enroll' "
            "AND state='awaiting_approval' AND expires_at>? ORDER BY created_at,grant_id",
            (client_id, now),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_challenge(self, conn: Any, values: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO box_auth_challenges(challenge_id,client_id,grant_id,action,params_json,"
            "payload_json,public_key_json,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            tuple(
                values[key]
                for key in (
                    "challenge_id",
                    "client_id",
                    "grant_id",
                    "action",
                    "params_json",
                    "payload_json",
                    "public_key_json",
                    "expires_at",
                )
            ),
        )

    def challenge(self, conn: Any, challenge_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_auth_challenges WHERE challenge_id=?", (challenge_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def consume_challenge(self, conn: Any, challenge_id: str, now: int) -> bool:
        cur = conn.execute(
            "UPDATE box_auth_challenges SET consumed_at=? WHERE challenge_id=? "
            "AND consumed_at IS NULL AND expires_at>?",
            (now, challenge_id, now),
        )
        return int(cur.rowcount) == 1

    def clean_challenges(self, conn: Any, now: int) -> None:
        conn.execute("DELETE FROM box_auth_challenges WHERE expires_at<=?", (now,))
        conn.execute("DELETE FROM box_auth_sessions WHERE expires_at<=?", (now,))
        conn.execute("DELETE FROM box_admin_sessions WHERE expires_at<=?", (now,))

    def challenge_count(self, conn: Any) -> int:
        return int(conn.execute("SELECT COUNT(*) AS n FROM box_auth_challenges").fetchone()["n"])

    def create_session(
        self,
        conn: Any,
        *,
        session_id: str,
        client_id: str,
        owner_id: int,
        revision: int,
        role: str,
        boot_id: str,
        now: int,
        expires_at: int,
    ) -> None:
        conn.execute(
            "INSERT INTO box_auth_sessions(session_id,client_id,owner_user_id,auth_revision,"
            "boot_id,created_at,expires_at,client_role) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, client_id, owner_id, revision, boot_id, now, expires_at, role),
        )
        conn.execute(
            "UPDATE box_paired_clients SET last_seen_at=? WHERE client_id=?", (now, client_id)
        )

    def session(self, conn: Any, session_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_auth_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def revoke_session(self, conn: Any, session_id: str, now: int) -> None:
        conn.execute(
            "UPDATE box_auth_sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
            (now, session_id),
        )

    def create_admin_session(
        self,
        conn: Any,
        *,
        session_id: str,
        user_id: int,
        revision: int,
        key_revision: int,
        boot_id: str,
        now: int,
        expires_at: int,
    ) -> None:
        conn.execute(
            "INSERT INTO box_admin_sessions(session_id,user_id,auth_revision,management_key_revision,"
            "boot_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
            (session_id, user_id, revision, key_revision, boot_id, now, expires_at),
        )

    def admin_session(self, conn: Any, session_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM box_admin_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def revoke_admin_session(self, conn: Any, session_id: str, now: int) -> None:
        conn.execute(
            "UPDATE box_admin_sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
            (now, session_id),
        )
