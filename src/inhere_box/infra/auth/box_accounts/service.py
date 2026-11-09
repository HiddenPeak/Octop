"""Single-owner pairing and revocable client sessions.

All public operations are synchronous and must run outside the event loop. One
daemon owns the authentication trust directory. Trusted local key maintenance
must acquire the same OS lock while the daemon is stopped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt

from inhere_box.infra.auth.box_accounts.crypto import (
    AUDIENCE,
    canonical,
    digest,
    parse_public_key,
    proof_challenge_id,
    verify_proof,
)
from inhere_box.infra.auth.box_accounts.trust import TrustStore
from inhere_box.infra.db.services import SharedServices
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.users.identity import User
from inhere_box.infra.utils.ulid import new_ulid

SESSION_TTL = 600
ADMIN_SESSION_TTL = 86400
CHALLENGE_TTL = 60
CLAIM_TTL = 300
SHARE_TTL = 48 * 60 * 60


@dataclass(frozen=True)
class BoxAuthContext:
    user: User
    session_id: str
    paired_client_id: str | None
    auth_revision: int
    session_kind: str = "client"
    role: str = "primary"
    management_key_revision: int = 0


def _error(reason: str, status: int = 401) -> InhereBoxError:
    code = ErrorCode.AUTH_FAILED if status == 401 else ErrorCode.FORBIDDEN
    return InhereBoxError.localized(code, status=status, details={"reason": reason})


def _user(row: dict[str, Any]) -> User:
    return User(
        id=int(row["id"]),
        username=str(row["username"]),
        role=str(row["role"]),
        display_name=row["display_name"],
        locale=str(row["locale"]),
        permissions=json.loads(row["permissions"] or "[]"),
    )


class BoxAccountService:
    def __init__(
        self,
        services: SharedServices,
        *,
        secret: bytes,
        box_id: str,
        trust_path: Path | None = None,
        boot_id: str | None = None,
    ) -> None:
        self._services = services
        self._repo = services.box_account_repo
        self._secret = secret
        self.box_id = box_id
        self.boot_id = boot_id or secrets.token_urlsafe(32)
        self._trust = TrustStore(trust_path or services.paths.root / "box-auth" / "trust.json")
        self._lock = threading.RLock()
        self._blocked = False
        self._pending: dict[str, Any] | None = None
        self._rate_buckets: dict[str, tuple[int, int]] = {}

    def _record(self, conn: Any, *, legacy: bool = False) -> dict[str, Any]:
        account = self._repo.account(conn)
        if account is None:
            raise _error("trust_unavailable", 503)
        content = {
            "box_id": account["box_id"],
            "owner_user_id": account["owner_user_id"],
            "auth_revision": account["auth_revision"],
            "clients": self._repo.trust_clients(conn, legacy=legacy),
        }
        if not legacy:
            content.update(
                auth_model_version=account["auth_model_version"],
                primary_client_id=account["primary_client_id"],
                management_key_hash=account["management_key_hash"],
                management_key_revision=account["management_key_revision"],
                principals=self._repo.trust_principals(conn),
            )
        return {
            "version": 1 if legacy else 2,
            "box_id": account["box_id"],
            "owner_user_id": account["owner_user_id"],
            "auth_revision": account["auth_revision"],
            "digest": digest(content),
        }

    def initialize(self) -> None:
        with self._lock, self._services.db.transaction() as conn:
            self._repo.initialize(conn, self.box_id)
            account = self._repo.account(conn, lock=True)
            assert account is not None
            pristine = (
                account["owner_user_id"] is None
                and account["auth_revision"] == 0
                and self._repo.owner_count(conn) == 0
                and not self._repo.trust_clients(conn)
                and not account["trust_digest"]
                and not account["management_key_hash"]
                and account["management_key_revision"] == 0
                and account["primary_client_id"] is None
            )
            try:
                if pristine and not self._trust.path.exists() and not self._trust.journal.exists():
                    self._repo.set_auth_model(conn, 2)
                    account = self._repo.account(conn)
                    assert account is not None
                    record = self._record(conn)
                    self._trust.initialize(record)
                if account["auth_model_version"] != 2:
                    raise ValueError("trusted local upgrade required")
                record = self._record(conn)
                saved = self._trust.read()
                if account["box_id"] != self.box_id or saved != record:
                    raise ValueError("trust mismatch")
                if pristine:
                    self._repo.save_trust(conn, revision=0, digest=record["digest"])
                elif account["trust_digest"] != record["digest"]:
                    raise ValueError("database trust mismatch")
                if account["owner_user_id"] is None and self._repo.owner_count(conn):
                    raise ValueError("existing users require explicit migration")
            except (OSError, ValueError):
                self._blocked = True

    def _ensure_trust(self, conn: Any) -> dict[str, Any]:
        if self._blocked:
            raise _error("recovery_required", 503)
        account = self._repo.account(conn, lock=True)
        try:
            record = self._record(conn)
            if (
                account is None
                or account["auth_model_version"] != 2
                or account["box_id"] != self.box_id
                or account["trust_digest"] != record["digest"]
                or (self._pending if self._pending is not None else self._trust.read()) != record
                or (account["owner_user_id"] is None and self._repo.owner_count(conn))
            ):
                raise ValueError("trust mismatch")
        except (OSError, ValueError):
            self._blocked = True
            raise _error("recovery_required", 503) from None
        assert account is not None
        return account

    @contextmanager
    def _transaction(self, *, check_trust: bool = True) -> Iterator[Any]:
        with self._lock:
            try:
                with self._services.db.transaction() as conn:
                    if check_trust:
                        self._ensure_trust(conn)
                    yield conn
                if self._pending is not None:
                    self._trust.publish(self._pending)
                    self._pending = None
            except BaseException:
                if self._pending is not None:
                    self._blocked = True
                    self._pending = None
                raise

    def _stage_trust(self, conn: Any) -> None:
        previous = self._trust.read()
        self._repo.save_trust(conn, revision=int(previous["auth_revision"]) + 1, digest="")
        target = self._record(conn)
        self._repo.save_trust(conn, revision=target["auth_revision"], digest=target["digest"])
        # Remember intent before writing so even a partial filesystem failure
        # fences this process. Restart likewise refuses an unfinished journal.
        self._pending = target
        self._trust.stage(previous, target)

    def rate_limit(self, source: str) -> None:
        now = int(time.time()) // 60
        with self._lock:
            self._rate_buckets = {
                key: value for key, value in self._rate_buckets.items() if value[0] == now
            }
            key = hashlib.sha256(source.encode()).hexdigest()
            if len(self._rate_buckets) >= 4096 and key not in self._rate_buckets:
                raise _error("rate_limited", 429)
            for bucket, maximum in (("global", 240), (key, 30)):
                count = self._rate_buckets.get(bucket, (now, 0))[1]
                if count >= maximum:
                    raise _error("rate_limited", 429)
                self._rate_buckets[bucket] = (now, count + 1)

    def status(self) -> dict[str, Any]:
        with self._lock, self._services.db.transaction() as conn:
            try:
                account = self._ensure_trust(conn)
                state = "claimed" if account["management_key_hash"] else "unclaimed"
                available = self._repo.pending_grants(conn, int(time.time())) > 0
                primary = account["primary_client_id"]
                configured = bool(account["management_key_hash"])
            except InhereBoxError:
                state, available, primary, configured = "recovery_required", False, None, False
        return {
            "account_mode": "box_local",
            "protocol_version": 1,
            "box_id": self.box_id,
            "state": state,
            "pairing_available": available,
            "primary_client_id": primary,
            "management_key_configured": configured,
            "auth_model_version": 2,
            "multiple_owner_devices": True,
        }

    def _upgrade_legacy(self, conn: Any) -> None:
        account = self._repo.account(conn, lock=True)
        if account is None or account["auth_model_version"] != 1:
            raise _error("legacy_upgrade_required", 409)
        try:
            record = self._record(conn, legacy=True)
            if (
                self._trust.read() != record
                or account["box_id"] != self.box_id
                or account["trust_digest"] != record["digest"]
                or account["management_key_hash"]
                or account["management_key_revision"] != 0
                or account["primary_client_id"] is not None
                or any(
                    c["role"] != "legacy" or c["guest_user_id"] is not None
                    for c in self._repo.clients(conn)
                )
                or (account["owner_user_id"] is None and self._repo.owner_count(conn))
            ):
                raise ValueError("legacy trust mismatch")
        except (OSError, ValueError):
            raise _error("recovery_required", 503) from None
        if account["owner_user_id"] is not None:
            owner = self._repo.owner(conn, account["owner_user_id"])
            if owner is None or owner["disabled"] or owner["role"] != "admin":
                raise _error("recovery_required", 503)
        for client in self._repo.clients(conn):
            self._repo.revoke_client(conn, client["client_id"], int(time.time()))
        self._repo.close_all_grants(conn)
        self._repo.set_auth_model(conn, 2)

    def initialize_management_key(self, *, upgrade_legacy: bool = False) -> dict[str, Any]:
        """Trusted local operation; the CLI must hold the stopped daemon's OS lock."""
        with self._transaction(check_trust=False) as conn:
            account = self._repo.account(conn, lock=True)
            if account is None:
                raise _error("initialization_required", 409)
            if account["auth_model_version"] == 1:
                if not upgrade_legacy:
                    raise _error("legacy_upgrade_required", 409)
                self._upgrade_legacy(conn)
            else:
                self._ensure_trust(conn)
            if account["management_key_hash"]:
                raise _error("management_key_already_initialized", 409)
            if account["owner_user_id"] is None:
                if self._repo.owner_count(conn):
                    raise _error("box_migration_required", 409)
                self._repo.create_owner(conn, int(time.time()))
            result = self._replace_management_key(conn, revoke_clients=False)
        self._blocked = False
        return result

    def _replace_management_key(self, conn: Any, *, revoke_clients: bool) -> dict[str, Any]:
        management_key = secrets.token_urlsafe(32)
        self._repo.set_management_key(conn, hashlib.sha256(management_key.encode()).hexdigest())
        self._repo.revoke_admin_sessions(conn, int(time.time()))
        self._repo.close_all_grants(conn)
        if revoke_clients:
            for client in self._repo.clients(conn):
                self._repo.revoke_client(conn, client["client_id"], int(time.time()))
            self._repo.set_primary(conn, None)
        self._stage_trust(conn)
        account = self._repo.account(conn)
        assert account is not None
        self._services.audit_repo.write_in_transaction(
            conn,
            actor="_management",
            action="box.management_key.rotate",
            target=self.box_id,
            payload=canonical(
                {"revoke_clients": revoke_clients, "revision": account["management_key_revision"]}
            ),
        )
        return {
            "management_key": management_key,
            "management_key_revision": account["management_key_revision"],
            "auth_revision": account["auth_revision"],
        }

    def rotate_management_key_local(self, *, revoke_clients: bool = False) -> dict[str, Any]:
        """Trusted stopped-daemon CLI operation; never expose without admin authorization."""
        with self._transaction() as conn:
            account = self._repo.account(conn)
            if account is None or not account["management_key_hash"]:
                raise _error("management_key_required", 409)
            return self._replace_management_key(conn, revoke_clients=revoke_clients)

    def rotate_management_key(
        self, context: BoxAuthContext, *, revoke_clients: bool = False
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_admin(context, conn)
            return self._replace_management_key(conn, revoke_clients=revoke_clients)

    def _verify_management_key(self, conn: Any, management_key: str) -> dict[str, Any]:
        account = self._repo.account(conn)
        if (
            account is None
            or not account["management_key_hash"]
            or not management_key
            or len(management_key) > 256
            or not hmac.compare_digest(
                account["management_key_hash"], hashlib.sha256(management_key.encode()).hexdigest()
            )
        ):
            raise _error("management_key_invalid")
        return account

    def owner_user(self) -> User | None:
        with self._transaction() as conn:
            account = self._repo.account(conn)
            assert account is not None
            if account["owner_user_id"] is None:
                return None
            row = self._repo.owner(conn, account["owner_user_id"])
            return (
                _user(row)
                if row is not None and not row["disabled"] and row["role"] == "admin"
                else None
            )

    def _new_grant(
        self,
        conn: Any,
        *,
        purpose: str,
        issued_by: str | None,
        ttl_seconds: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        if self._repo.pending_grants(conn, now) >= 32:
            raise _error("pairing_capacity", 429)
        grant_id, secret = new_ulid(), secrets.token_urlsafe(32)
        expires_at = now + ttl_seconds
        self._repo.create_grant(
            conn,
            grant_id=grant_id,
            purpose=purpose,
            secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
            expires_at=expires_at,
            now=now,
            issued_by=issued_by,
            request_id=request_id,
        )
        self._services.audit_repo.write_in_transaction(
            conn,
            actor=issued_by or "_local",
            action="box.grant.create",
            target=grant_id,
            payload=canonical({"purpose": purpose}),
        )
        return {
            "grant_id": grant_id,
            "purpose": purpose,
            "secret": secret,
            "expires_at": expires_at,
        }

    def create_claim_grant(self, *, ttl_seconds: int = CLAIM_TTL) -> dict[str, Any]:
        raise _error("management_key_required", 409)

    @staticmethod
    def _candidate(
        public_key_jwk: dict[str, Any], request_id: str, name: str, platform: str
    ) -> tuple[str, str]:
        try:
            public_key_json, fingerprint = parse_public_key(public_key_jwk)
        except (ValueError, TypeError, jwt.PyJWTError):
            raise _error("public_key_invalid", 400) from None
        if not request_id or len(request_id) > 128 or not name.strip() or len(name) > 128:
            raise _error("pairing_parameters_invalid", 400)
        if not platform or len(platform) > 64:
            raise _error("pairing_parameters_invalid", 400)
        return public_key_json, fingerprint

    def _bind_candidate(
        self,
        conn: Any,
        grant: dict[str, Any],
        *,
        request_id: str,
        public_key_json: str,
        fingerprint: str,
        name: str,
        platform: str,
    ) -> dict[str, Any]:
        grant_id = grant["grant_id"]
        request_digest = digest([grant_id, fingerprint, name, platform])
        previous = self._repo.grant_by_request(conn, request_id)
        if previous is not None and previous["request_digest"] != request_digest:
            raise _error("request_conflict", 409)
        if grant["state"] != "issued":
            if grant["request_id"] != request_id or grant["request_digest"] != request_digest:
                raise _error("candidate_already_bound", 409)
        else:
            if self._repo.client_by_key(conn, fingerprint) is not None:
                raise _error("public_key_already_registered", 409)
            self._repo.bind_candidate(
                conn,
                grant_id=grant_id,
                request_id=request_id,
                request_digest=request_digest,
                public_key_json=public_key_json,
                fingerprint=fingerprint,
                name=name,
                platform=platform,
            )
            grant = self._usable_grant(conn, grant_id)
        result = self._pairing(grant)
        result["challenge"] = self._challenge(
            conn, client_id=None, grant=grant, action="pair", params={}
        )
        return result

    def create_primary_claim(
        self,
        *,
        management_key: str,
        request_id: str,
        public_key_jwk: dict[str, Any],
        name: str,
        platform: str,
    ) -> dict[str, Any]:
        public_key_json, fingerprint = self._candidate(public_key_jwk, request_id, name, platform)
        with self._transaction() as conn:
            self._verify_management_key(conn, management_key)
            grant = self._repo.grant_by_request(conn, request_id)
            if grant is None:
                created = self._new_grant(
                    conn, purpose="claim", issued_by=None, ttl_seconds=CLAIM_TTL
                )
                grant = self._usable_grant(conn, created["grant_id"])
            else:
                grant = self._usable_grant(conn, grant["grant_id"])
                if grant["purpose"] != "claim":
                    raise _error("request_conflict", 409)
            return self._bind_candidate(
                conn,
                grant,
                request_id=request_id,
                public_key_json=public_key_json,
                fingerprint=fingerprint,
                name=name,
                platform=platform,
            )

    def _usable_grant(self, conn: Any, grant_id: str) -> dict[str, Any]:
        grant = self._repo.grant(conn, grant_id)
        if (
            grant is None
            or grant["state"] in ("cancelled", "consumed")
            or grant["expires_at"] <= int(time.time())
            or grant["failed_attempts"] >= 10
        ):
            raise _error("pairing_invalid")
        if grant["issued_by_client_id"] is not None:
            client = self._repo.client(conn, grant["issued_by_client_id"])
            if client is None or client["state"] != "active" or client["role"] != "primary":
                raise _error("pairing_invalid")
        return grant

    @staticmethod
    def _pairing(grant: dict[str, Any]) -> dict[str, Any]:
        return {
            "pairing_id": grant["grant_id"],
            "state": grant["state"],
            "public_key_fingerprint": grant["candidate_fingerprint"],
            "client_id": grant["client_id"],
            "version": 1,
        }

    def create_pairing(
        self,
        *,
        grant_id: str | None = None,
        secret: str,
        request_id: str,
        public_key_jwk: dict[str, Any],
        name: str,
        platform: str,
    ) -> dict[str, Any]:
        public_key_json, fingerprint = self._candidate(public_key_jwk, request_id, name, platform)
        if len(secret) > 256:
            raise _error("pairing_parameters_invalid", 400)
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        failed_secret = False
        result: dict[str, Any] = {}
        with self._transaction() as conn:
            if grant_id is None:
                located = self._repo.grant_by_secret_hash(conn, secret_hash)
                if located is None:
                    raise _error("pairing_invalid")
                grant_id = str(located["grant_id"])
            grant = self._usable_grant(conn, grant_id)
            if grant["purpose"] != "enroll":
                raise _error("pairing_invalid")
            if not hmac.compare_digest(grant["secret_hash"], secret_hash):
                self._repo.fail_grant(conn, grant_id)
                failed_secret = True
            else:
                result = self._bind_candidate(
                    conn,
                    grant,
                    request_id=request_id,
                    public_key_json=public_key_json,
                    fingerprint=fingerprint,
                    name=name,
                    platform=platform,
                )
        if failed_secret:
            raise _error("pairing_invalid")
        return result

    def _challenge(
        self,
        conn: Any,
        *,
        client_id: str | None,
        grant: dict[str, Any] | None,
        action: str,
        params: dict[str, Any],
        persist: bool = True,
        pairing_id: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        self._repo.clean_challenges(conn, now)
        if self._repo.challenge_count(conn) >= 512:
            raise _error("challenge_capacity", 429)
        account = self._repo.account(conn)
        assert account is not None
        public_key_json = ""
        if client_id is not None:
            client = self._repo.client(conn, client_id)
            if client is None or (
                client["state"] != "active" and action not in {"login", "client-status"}
            ):
                persist = False
            else:
                public_key_json = client["public_key_json"]
        elif grant is not None:
            public_key_json = grant["candidate_key_json"]
        challenge_id = new_ulid()
        payload = {
            "box_id": self.box_id,
            "challenge_id": challenge_id,
            "nonce": secrets.token_urlsafe(32),
            "action": action,
            "params_hash": digest(params),
            "subject": "client:" + client_id
            if client_id is not None
            else "pairing:" + str(grant["grant_id"] if grant else pairing_id or ""),
            "auth_revision": account["auth_revision"],
            "boot_id": self.boot_id,
            "exp": now + CHALLENGE_TTL,
        }
        if persist:
            self._repo.add_challenge(
                conn,
                {
                    "challenge_id": challenge_id,
                    "client_id": client_id,
                    "grant_id": grant["grant_id"] if grant else None,
                    "action": action,
                    "params_json": canonical(params),
                    "payload_json": canonical(payload),
                    "public_key_json": public_key_json,
                    "expires_at": now + CHALLENGE_TTL,
                },
            )
        return {"challenge_id": challenge_id, "payload": payload, "expires_at": now + CHALLENGE_TTL}

    def create_challenge(
        self,
        *,
        client_id: str | None = None,
        pairing_id: str | None = None,
        action: str = "session",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        if len(canonical(params)) > 1024 or (client_id is None) == (pairing_id is None):
            raise _error("challenge_parameters_invalid", 400)
        if client_id is not None and action not in {
            "session",
            "login",
            "client-status",
            "enroll",
            "approve",
            "cancel",
            "revoke",
        }:
            raise _error("challenge_parameters_invalid", 400)
        if pairing_id is not None and action != "result":
            raise _error("challenge_parameters_invalid", 400)
        if action == "revoke":
            target = params.get("target_client_id")
            if (
                set(params) != {"target_client_id"}
                or not isinstance(target, str)
                or not 1 <= len(target) <= 128
            ):
                raise _error("challenge_parameters_invalid", 400)
        with self._transaction() as conn:
            grant = self._repo.grant(conn, pairing_id) if pairing_id is not None else None
            persist = pairing_id is None or (
                grant is not None and bool(grant["candidate_key_json"])
            )
            return self._challenge(
                conn,
                client_id=client_id,
                grant=grant,
                action=action,
                params=params,
                persist=persist,
                pairing_id=pairing_id,
            )

    def _proof(
        self,
        conn: Any,
        proof: str,
        *,
        action: str,
        params: dict[str, Any],
        client_id: str | None = None,
        grant_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            challenge = self._repo.challenge(conn, proof_challenge_id(proof))
            account = self._repo.account(conn)
            if challenge is None or account is None:
                raise ValueError("unknown challenge")
            payload = json.loads(challenge["payload_json"])
            if (
                challenge["consumed_at"] is not None
                or challenge["action"] != action
                or challenge["params_json"] != canonical(params)
                or (client_id is not None and challenge["client_id"] != client_id)
                or (grant_id is not None and challenge["grant_id"] != grant_id)
                or payload["boot_id"] != self.boot_id
            ):
                raise ValueError("challenge mismatch")
            verify_proof(proof, challenge["public_key_json"], payload)
            now = int(time.time())
            if (
                challenge["expires_at"] <= now
                or payload["auth_revision"] != account["auth_revision"]
            ):
                # Only a correctly signed, unused proof for these actions gets a retry hint.
                if action in {"session", "login", "revoke"}:
                    raise _error("challenge_stale", 409)
                raise ValueError("challenge stale")
            if not self._repo.consume_challenge(conn, challenge["challenge_id"], now):
                raise ValueError("challenge consumed")
            return challenge
        except (ValueError, TypeError, KeyError, jwt.PyJWTError):
            raise _error("proof_invalid") from None

    def _register(self, conn: Any, grant: dict[str, Any]) -> dict[str, Any]:
        account = self._repo.account(conn)
        assert account is not None
        now = int(time.time())
        owner_id = account["owner_user_id"]
        if owner_id is None or not account["management_key_hash"]:
            raise _error("management_key_required", 409)
        role = "guest"
        if grant["purpose"] == "claim":
            # Keep the primary wire role for existing clients; it now denotes
            # owner access and is no longer an exclusive device slot.
            role = "primary"
        if self._repo.client_by_key(conn, grant["candidate_fingerprint"]) is not None:
            raise _error("public_key_already_registered", 409)
        client_id = new_ulid()
        guest_id = self._repo.create_guest(conn, client_id, grant["candidate_name"], now)
        self._repo.create_client(
            conn,
            client_id=client_id,
            owner_id=owner_id,
            guest_user_id=guest_id,
            role=role,
            candidate=grant,
            now=now,
        )
        if role == "primary" and account["primary_client_id"] is None:
            self._repo.set_primary(conn, client_id)
        self._repo.grant_state(conn, grant["grant_id"], "consumed", client_id=client_id)
        self._services.audit_repo.write_in_transaction(
            conn,
            actor=grant["issued_by_client_id"] or "_management",
            action="box.client.pair",
            target=client_id,
            payload=canonical({"guest_user_id": guest_id, "role": role}),
        )
        result = self._repo.grant(conn, grant["grant_id"])
        assert result is not None
        self._stage_trust(conn)
        return self._pairing(result)

    def prove_pairing(self, pairing_id: str, proof: str) -> dict[str, Any]:
        with self._transaction() as conn:
            grant = self._usable_grant(conn, pairing_id)
            if grant["state"] != "candidate_bound":
                raise _error("pairing_invalid")
            self._proof(conn, proof, action="pair", params={}, grant_id=pairing_id)
            return self._register(conn, grant)

    def create_enroll_grant(
        self, context: BoxAuthContext, *, request_id: str, proof: str | None = None
    ) -> dict[str, Any]:
        if not request_id or len(request_id) > 128:
            raise _error("request_invalid", 400)
        with self._transaction() as conn:
            validated = self.validate_context(context, conn)
            if validated.role not in {"admin", "primary"}:
                raise _error("management_required", 403)
            if validated.session_kind == "client":
                self._proof(
                    conn,
                    proof or "",
                    action="enroll",
                    params={"request_id": request_id},
                    client_id=validated.paired_client_id,
                )
            if (
                self._repo.grant_by_issuance(conn, validated.paired_client_id, request_id)
                is not None
            ):
                raise _error("grant_request_already_completed", 409)
            return self._new_grant(
                conn,
                purpose="enroll",
                issued_by=validated.paired_client_id,
                ttl_seconds=SHARE_TTL,
                request_id=request_id,
            )

    def approve_pairing(
        self, context: BoxAuthContext, pairing_id: str, proof: str
    ) -> dict[str, Any]:
        raise _error("pairing_approval_retired", 410)

    def cancel_pairing(
        self, context: BoxAuthContext, pairing_id: str, proof: str | None = None
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            validated = self.validate_context(context, conn)
            if validated.role not in {"admin", "primary"}:
                raise _error("management_required", 403)
            grant = self._usable_grant(conn, pairing_id)
            if validated.session_kind == "client":
                if grant["issued_by_client_id"] != validated.paired_client_id:
                    raise _error("pairing_approver_mismatch", 403)
                self._proof(
                    conn,
                    proof or "",
                    action="cancel",
                    params={"pairing_id": pairing_id},
                    client_id=validated.paired_client_id,
                )
            self._repo.grant_state(conn, pairing_id, "cancelled")
            self._services.audit_repo.write_in_transaction(
                conn,
                actor=validated.paired_client_id or "_management",
                action="box.grant.cancel",
                target=pairing_id,
            )
            self._stage_trust(conn)
            grant["state"] = "cancelled"
            return self._pairing(grant)

    def pairing_result(self, pairing_id: str, proof: str) -> dict[str, Any]:
        with self._transaction() as conn:
            self._proof(conn, proof, action="result", params={}, grant_id=pairing_id)
            grant = self._repo.grant(conn, pairing_id)
            if grant is None:
                raise _error("pairing_invalid")
            result = self._pairing(grant)
            if grant["state"] not in {"consumed", "cancelled"} and grant["expires_at"] <= int(
                time.time()
            ):
                result["state"] = "expired"
            return result

    def _client_user(self, conn: Any, client: dict[str, Any], account: dict[str, Any]) -> User:
        if client["state"] != "active" or client["owner_user_id"] != account["owner_user_id"]:
            raise _error("session_invalid")
        role = client["role"]
        if role == "primary":
            user_id = account["owner_user_id"]
        elif role == "guest" and client["guest_user_id"] is not None:
            user_id = client["guest_user_id"]
        else:
            raise _error("session_invalid")
        row = self._repo.owner(conn, user_id)
        if row is None or row["disabled"]:
            raise _error("session_invalid")
        user = _user(row)
        if (role == "primary" and not user.is_admin) or (
            role == "guest" and (user.role != "user" or user.permissions)
        ):
            raise _error("session_invalid")
        return user

    def user_for_client(self, client_id: str) -> User:
        with self._transaction() as conn:
            account = self._repo.account(conn)
            client = self._repo.client(conn, client_id)
            if account is None or client is None:
                raise _error("session_invalid")
            return self._client_user(conn, client, account)

    def _token(
        self,
        *,
        user: User,
        sid: str,
        client_id: str | None,
        revision: int,
        session_kind: str,
        role: str,
        key_revision: int,
        now: int,
        ttl: int,
    ) -> dict[str, Any]:
        claims = {
            "sub": str(user.id),
            "uname": user.username,
            "role": user.role,
            "iss": self.box_id,
            "aud": AUDIENCE,
            "sid": sid,
            "paired_client_id": client_id,
            "auth_revision": revision,
            "boot_id": self.boot_id,
            "iat": now,
            "exp": now + ttl,
            "session_kind": session_kind,
            "box_role": role,
            "management_key_revision": key_revision,
        }
        return {
            "access_token": jwt.encode(claims, self._secret, algorithm="HS256"),
            "token_type": "bearer",
            "expires_in": ttl,
            "session_id": sid,
            "paired_client_id": client_id,
            "session_kind": session_kind,
            "role": role,
            "user_id": user.id,
        }

    def client_status(self, client_id: str, proof: str) -> dict[str, Any]:
        """A status proof cannot restore a device or obtain a session."""
        with self._transaction() as conn:
            self._proof(conn, proof, action="client-status", params={}, client_id=client_id)
            client = self._repo.client(conn, client_id)
            if client is None or client["state"] not in {"active", "revoked"}:
                raise _error("client_not_found", 404)
            return {
                "box_id": self.box_id,
                "client_id": client_id,
                "state": client["state"],
                "revoked_at": client["revoked_at"],
            }

    def login_client(self, proof: str, secret: str) -> dict[str, Any]:
        """Fresh credentials can restore a revoked device without changing its account."""
        if not secret or len(secret) > 512:
            raise _error("credential_invalid")
        with self._transaction() as conn:
            challenge = self._proof(
                conn, proof, action="login", params={"secret_hash": digest(secret)}
            )
            client = self._repo.client(conn, challenge["client_id"] or "")
            account = self._repo.account(conn)
            if client is None or account is None:
                raise _error("proof_invalid")
            if client["role"] == "primary":
                self._verify_management_key(conn, secret)
            else:
                located = self._repo.grant_by_secret_hash(
                    conn, hashlib.sha256(secret.encode()).hexdigest()
                )
                if located is None:
                    raise _error("credential_invalid")
                grant = self._usable_grant(conn, located["grant_id"])
                if grant["purpose"] != "enroll" or grant["state"] != "issued":
                    raise _error("credential_invalid")
                self._repo.grant_state(
                    conn, grant["grant_id"], "consumed", client_id=client["client_id"]
                )
            restored = client["state"] == "revoked"
            if restored:
                self._repo.restore_client(conn, client["client_id"])
                if client["role"] == "primary" and account["primary_client_id"] is None:
                    self._repo.set_primary(conn, client["client_id"])
                client = self._repo.client(conn, client["client_id"])
                assert client is not None
            result = self._client_session(conn, client)
            if restored:
                self._services.audit_repo.write_in_transaction(
                    conn,
                    actor=client["client_id"],
                    action="box.client.restore",
                    target=client["client_id"],
                )
            if restored or client["role"] == "guest":
                self._stage_trust(conn)
            return result

    def create_session(self, proof: str) -> dict[str, Any]:
        with self._transaction() as conn:
            challenge = self._proof(conn, proof, action="session", params={})
            client = self._repo.client(conn, challenge["client_id"] or "")
            if client is None:
                raise _error("proof_invalid")
            return self._client_session(conn, client)

    def _client_session(self, conn: Any, client: dict[str, Any]) -> dict[str, Any]:
        account = self._repo.account(conn)
        if account is None:
            raise _error("proof_invalid")
        user = self._client_user(conn, client, account)
        now, sid = int(time.time()), new_ulid()
        self._repo.create_session(
            conn,
            session_id=sid,
            client_id=client["client_id"],
            owner_id=user.id,
            revision=account["auth_revision"],
            role=client["role"],
            boot_id=self.boot_id,
            now=now,
            expires_at=now + SESSION_TTL,
        )
        result = self._token(
            user=user,
            sid=sid,
            client_id=client["client_id"],
            revision=account["auth_revision"],
            session_kind="client",
            role=client["role"],
            key_revision=0,
            now=now,
            ttl=SESSION_TTL,
        )
        result["account"] = self._account_view(conn, account, user, client["role"], "client")
        return result

    def _csrf_token(self, session_id: str) -> str:
        message = canonical(["inhere-box-browser-csrf-v1", self.box_id, self.boot_id, session_id])
        return hmac.new(self._secret, message.encode(), hashlib.sha256).hexdigest()

    def require_browser_csrf(self, context: BoxAuthContext, token: str) -> None:
        if (
            context.session_kind != "admin"
            or len(token) != 64
            or not token.isascii()
            or not hmac.compare_digest(self._csrf_token(context.session_id), token)
        ):
            raise _error("box_csrf_invalid", 403)

    def _create_admin_session(self, conn: Any) -> dict[str, Any]:
        account = self._repo.account(conn)
        assert account is not None
        row = self._repo.owner(conn, account["owner_user_id"])
        if row is None or row["disabled"] or row["role"] != "admin":
            raise _error("session_invalid")
        user = _user(row)
        now, sid = int(time.time()), new_ulid()
        self._repo.create_admin_session(
            conn,
            session_id=sid,
            user_id=user.id,
            revision=account["auth_revision"],
            key_revision=account["management_key_revision"],
            boot_id=self.boot_id,
            now=now,
            expires_at=now + ADMIN_SESSION_TTL,
        )
        result = self._token(
            user=user,
            sid=sid,
            client_id=None,
            revision=account["auth_revision"],
            session_kind="admin",
            role="admin",
            key_revision=account["management_key_revision"],
            now=now,
            ttl=ADMIN_SESSION_TTL,
        )
        result["csrf_token"] = self._csrf_token(sid)
        return result

    def create_admin_session(
        self, management_key: str, previous: BoxAuthContext | None = None
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            self._verify_management_key(conn, management_key)
            if previous is not None:
                self._require_admin(previous, conn)
                self._repo.revoke_admin_session(conn, previous.session_id, int(time.time()))
                self._stage_trust(conn)
            return self._create_admin_session(conn)

    def refresh_admin_session(self, context: BoxAuthContext) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_admin(context, conn)
            self._repo.revoke_admin_session(conn, context.session_id, int(time.time()))
            self._stage_trust(conn)
            return self._create_admin_session(conn)

    def browser_session(self, context: BoxAuthContext) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_admin(context, conn)
            session = self._repo.admin_session(conn, context.session_id)
            assert session is not None
            return {
                "session_id": context.session_id,
                "paired_client_id": None,
                "expires_in": session["expires_at"] - int(time.time()),
                "csrf_token": self._csrf_token(context.session_id),
                "session_kind": "admin",
                "role": "admin",
            }

    def _require_admin(self, context: BoxAuthContext, conn: Any) -> BoxAuthContext:
        validated = self.validate_context(context, conn)
        if validated.session_kind != "admin" or validated.role != "admin":
            raise _error("management_required", 403)
        return validated

    def validate_context(self, context: BoxAuthContext, conn: Any | None = None) -> BoxAuthContext:
        if conn is None:
            with self._transaction() as transaction:
                return self.validate_context(context, transaction)
        account = self._ensure_trust(conn)
        if context.session_kind == "admin":
            session = self._repo.admin_session(conn, context.session_id)
            owner = self._repo.owner(conn, context.user.id)
            if (
                context.paired_client_id is not None
                or context.role != "admin"
                or owner is None
                or owner["disabled"]
                or owner["role"] != "admin"
                or account["owner_user_id"] != context.user.id
                or session is None
                or session["user_id"] != context.user.id
                or session["management_key_revision"] != context.management_key_revision
                or context.management_key_revision != account["management_key_revision"]
            ):
                raise _error("session_invalid")
            user = _user(owner)
        elif context.session_kind == "client" and context.paired_client_id is not None:
            session = self._repo.session(conn, context.session_id)
            client = self._repo.client(conn, context.paired_client_id)
            if (
                client is None
                or session is None
                or session["client_id"] != context.paired_client_id
                or session["owner_user_id"] != context.user.id
                or session["client_role"] != context.role
                or client["role"] != context.role
                or context.management_key_revision != 0
            ):
                raise _error("session_invalid")
            user = self._client_user(conn, client, account)
            if user.id != context.user.id:
                raise _error("session_invalid")
        else:
            raise _error("session_invalid")
        if (
            session["revoked_at"] is not None
            or session["expires_at"] <= int(time.time())
            or session["boot_id"] != self.boot_id
            or session["auth_revision"] != context.auth_revision
            or context.auth_revision > account["auth_revision"]
        ):
            raise _error("session_invalid")
        return BoxAuthContext(
            user,
            context.session_id,
            context.paired_client_id,
            context.auth_revision,
            context.session_kind,
            context.role,
            context.management_key_revision,
        )

    def validate_access_token(self, token: str) -> BoxAuthContext:
        try:
            if len(token) > 8192:
                raise ValueError("token size")
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=AUDIENCE,
                issuer=self.box_id,
                options={
                    "require": [
                        "sub",
                        "iat",
                        "exp",
                        "sid",
                        "auth_revision",
                        "boot_id",
                        "session_kind",
                        "box_role",
                        "management_key_revision",
                    ]
                },
            )
            if (
                claims["boot_id"] != self.boot_id
                or type(claims["auth_revision"]) is not int
                or type(claims["management_key_revision"]) is not int
                or (
                    claims["paired_client_id"] is not None
                    and not isinstance(claims["paired_client_id"], str)
                )
            ):
                raise ValueError("token epoch")
            context = BoxAuthContext(
                User(id=int(claims["sub"]), username="", role="", display_name=None),
                session_id=str(claims["sid"]),
                paired_client_id=claims["paired_client_id"],
                auth_revision=claims["auth_revision"],
                session_kind=claims["session_kind"],
                role=claims["box_role"],
                management_key_revision=claims["management_key_revision"],
            )
        except (ValueError, TypeError, KeyError, jwt.PyJWTError):
            raise _error("session_invalid") from None
        return self.validate_context(context)

    def account(self, context: BoxAuthContext) -> dict[str, Any]:
        with self._transaction() as conn:
            validated = self.validate_context(context, conn)
            account = self._repo.account(conn)
            assert account is not None
            return self._account_view(
                conn, account, validated.user, validated.role, validated.session_kind
            )

    def _account_view(
        self, conn: Any, account: dict[str, Any], user: User, role: str, session_kind: str
    ) -> dict[str, Any]:
        identity = {"id": user.id, "username": user.username, "display_name": user.display_name}
        is_owner = role in {"admin", "primary"}
        return {
            "box_id": self.box_id,
            "user": identity,
            "owner": identity if is_owner else None,
            "auth_revision": account["auth_revision"],
            "recovery_key_confirmed": False,
            "role": role,
            "session_kind": session_kind,
            "primary_client_id": account["primary_client_id"],
            "owner_client_ids": self._repo.owner_client_ids(conn) if is_owner else [],
        }

    def clients(self, context: BoxAuthContext) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            validated = self.validate_context(context, conn)
            if validated.role not in {"admin", "primary"}:
                raise _error("management_required", 403)
            return [
                {
                    key: client[key]
                    for key in (
                        "client_id",
                        "name",
                        "platform",
                        "public_key_fingerprint",
                        "state",
                        "created_at",
                        "last_seen_at",
                        "revoked_at",
                        "role",
                        "guest_user_id",
                    )
                }
                for client in self._repo.clients(conn)
            ]

    def pending_pairings(self, context: BoxAuthContext) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            self._require_admin(context, conn)
            return []

    def revoke_client(
        self, context: BoxAuthContext, client_id: str, proof: str | None = None
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            validated = self.validate_context(context, conn)
            if validated.session_kind == "admin" and validated.role == "admin":
                actor = "_management"
            elif (
                validated.session_kind == "client"
                and validated.role == "primary"
                and validated.paired_client_id is not None
            ):
                actor = validated.paired_client_id
                if actor == client_id:
                    raise _error("self_revocation_forbidden", 403)
                if proof is None:
                    raise _error("proof_required", 403)
                self._proof(
                    conn,
                    proof,
                    action="revoke",
                    params={"target_client_id": client_id},
                    client_id=actor,
                )
            else:
                raise _error("management_required", 403)
            client = self._repo.client(conn, client_id)
            account = self._repo.account(conn)
            assert account is not None
            if client is None:
                raise _error("client_not_found", 404)
            if client["state"] == "revoked":
                return {"client_id": client_id, "state": "revoked"}
            self._repo.revoke_client(conn, client_id, int(time.time()))
            if account["primary_client_id"] == client_id:
                remaining = self._repo.owner_client_ids(conn)
                self._repo.set_primary(conn, remaining[0] if remaining else None)
            self._services.audit_repo.write_in_transaction(
                conn, actor=actor, action="box.client.revoke", target=client_id
            )
            self._stage_trust(conn)
            return {"client_id": client_id, "state": "revoked"}

    def delete_client(self, context: BoxAuthContext, client_id: str) -> None:
        with self._transaction() as conn:
            self._require_admin(context, conn)
            client = self._repo.client(conn, client_id)
            if client is None:
                raise _error("client_not_found", 404)
            if client["state"] != "revoked":
                raise _error("client_not_revoked", 409)
            account = self._repo.account(conn)
            assert account is not None
            if account["primary_client_id"] == client_id:
                remaining = self._repo.owner_client_ids(conn)
                self._repo.set_primary(conn, remaining[0] if remaining else None)
            self._repo.delete_client(conn, client_id)
            self._services.audit_repo.write_in_transaction(
                conn, actor="_management", action="box.client.delete", target=client_id
            )
            self._stage_trust(conn)

    def set_primary(self, context: BoxAuthContext, client_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_admin(context, conn)
            raise _error("box_primary_transfer_retired", 410)

    def add_owner_device(self, context: BoxAuthContext, client_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_admin(context, conn)
            account = self._repo.account(conn)
            assert account is not None
            client = self._repo.client(conn, client_id)
            if client is None or client["state"] != "active" or client["guest_user_id"] is None:
                raise _error("client_not_found", 404)
            if client["role"] != "primary":
                now = int(time.time())
                self._repo.set_client_role(conn, client_id, "primary", now)
                if account["primary_client_id"] is None:
                    self._repo.set_primary(conn, client_id)
                self._stage_trust(conn)
                self._services.audit_repo.write_in_transaction(
                    conn, actor="_management", action="box.owner_device.add", target=client_id
                )
            return {
                "client_id": client_id,
                "owner_client_ids": self._repo.owner_client_ids(conn),
            }

    def logout(self, context: BoxAuthContext) -> dict[str, Any]:
        with self._transaction() as conn:
            validated = self.validate_context(context, conn)
            if validated.session_kind == "admin":
                self._repo.revoke_admin_session(conn, validated.session_id, int(time.time()))
            else:
                self._repo.revoke_session(conn, validated.session_id, int(time.time()))
            self._services.audit_repo.write_in_transaction(
                conn,
                actor=validated.paired_client_id or "_management",
                action="box.session.revoke",
                target=validated.session_id,
            )
            self._stage_trust(conn)
            return {"session_id": validated.session_id, "revoked": True}
