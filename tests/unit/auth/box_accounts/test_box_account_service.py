"""Real signatures, independent principals, management recovery and rollback fences."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from inhere_box.config import InhereBoxConfig
from inhere_box.infra.auth.box_accounts import BoxAccountService
from inhere_box.infra.auth.box_accounts.crypto import PROOF_TYPE, canonical
from inhere_box.infra.db import migrate
from inhere_box.infra.db.migrate import run_migrations
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.services import build_shared_services
from inhere_box.infra.errors import InhereBoxError
from inhere_box.infra.utils.paths import PathLayout


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INHERE_BOX_HOME", str(tmp_path))
    paths = PathLayout(tmp_path)
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(
        db=db, paths=paths, config=InhereBoxConfig(account_mode="box_local")
    )
    domain = BoxAccountService(services, secret=b"test-secret" * 4, box_id="test-box")
    domain.initialize()
    yield domain
    db.close()


@pytest.fixture
def management_key(service):
    return service.initialize_management_key()["management_key"]


@pytest.fixture
def box_clock(monkeypatch: pytest.MonkeyPatch):
    now = [int(time.time())]
    monkeypatch.setattr(
        "inhere_box.infra.auth.box_accounts.service.time", SimpleNamespace(time=lambda: now[0])
    )
    return now


def keypair() -> tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]:
    key = ec.generate_private_key(ec.SECP256R1())
    return key, json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))


def sign(key, challenge):
    return jwt.encode(challenge["payload"], key, algorithm="ES256", headers={"typ": PROOF_TYPE})


def admin(service, management_key):
    session = service.create_admin_session(management_key)
    return service.validate_access_token(session["access_token"])


def primary_candidate(service, management_key, request_id="primary"):
    key, public = keypair()
    pairing = service.create_primary_claim(
        management_key=management_key,
        request_id=request_id,
        public_key_jwk=public,
        name="Primary",
        platform="native",
    )
    return key, pairing


def claim(service, management_key):
    key, pairing = primary_candidate(service, management_key)
    result = service.prove_pairing(pairing["pairing_id"], sign(key, pairing["challenge"]))
    return key, result["client_id"]


def login(service, key, client_id):
    result = service.create_session(sign(key, service.create_challenge(client_id=client_id)))
    assert result["expires_in"] == 600
    return service.validate_access_token(result["access_token"]), result["access_token"]


def begin_pairing(service, grant, *, request_id="guest", explicit_id=False):
    key, public = keypair()
    pairing = service.create_pairing(
        **({"grant_id": grant["grant_id"]} if explicit_id else {}),
        secret=grant["secret"],
        request_id=request_id,
        public_key_jwk=public,
        name="Guest",
        platform="native",
    )
    return key, pairing


def enroll(service, context, request_id="guest"):
    grant = service.create_enroll_grant(context, request_id="issue-" + request_id)
    key, pairing = begin_pairing(service, grant, request_id=request_id)
    result = service.prove_pairing(pairing["pairing_id"], sign(key, pairing["challenge"]))
    assert result["state"] == "consumed"
    return key, result["client_id"]


def test_management_login_does_not_claim_any_client(service, management_key):
    session = service.create_admin_session(management_key)
    assert session["expires_in"] == 86400 and session["paired_client_id"] is None
    context = service.validate_access_token(session["access_token"])
    assert context.session_kind == "admin" and context.role == "admin" and context.user.is_admin
    assert service.clients(context) == []
    assert service.account(context)["primary_client_id"] is None
    with service._services.db.connect() as conn:
        row = service._repo.account(conn)
        assert row["management_key_hash"] == hashlib.sha256(management_key.encode()).hexdigest()
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert management_key not in repr(
        service._services.audit_repo.query(action="box.management_key.rotate")
    )
    with pytest.raises(InhereBoxError):
        service.initialize_management_key()
    with pytest.raises(InhereBoxError):
        service.create_admin_session("wrong")
    with pytest.raises(InhereBoxError):
        service.create_claim_grant()


def test_protocol_rename_preserves_registered_device_identity(service, management_key, monkeypatch):
    # Model an existing installation enrolled with the pre-rename protocol.
    with monkeypatch.context() as legacy:
        legacy.setattr(
            "inhere_box.infra.auth.box_accounts.crypto.PROOF_TYPE", "octop-box-proof+jwt"
        )
        legacy.setattr("inhere_box.infra.auth.box_accounts.service.AUDIENCE", "octop-box-api")
        key, pairing = primary_candidate(service, management_key)
        proof = jwt.encode(
            pairing["challenge"]["payload"],
            key,
            algorithm="ES256",
            headers={"typ": "octop-box-proof+jwt"},
        )
        client_id = service.prove_pairing(pairing["pairing_id"], proof)["client_id"]

    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    challenge = restarted.create_challenge(client_id=client_id)
    old_proof = jwt.encode(
        challenge["payload"], key, algorithm="ES256", headers={"typ": "octop-box-proof+jwt"}
    )
    with pytest.raises(InhereBoxError):
        restarted.create_session(old_proof)
    new_proof = jwt.encode(
        challenge["payload"], key, algorithm="ES256", headers={"typ": "inhere-box-proof+jwt"}
    )
    result = restarted.create_session(new_proof)
    claims = jwt.decode(result["access_token"], options={"verify_signature": False})
    assert claims["aud"] == "inhere-box-api"
    assert restarted.validate_access_token(result["access_token"]).paired_client_id == client_id


def test_first_primary_is_explicit_and_guest_users_are_independent(service, management_key):
    manager = admin(service, management_key)
    key, primary_id = claim(service, management_key)
    primary, _ = login(service, key, primary_id)
    assert primary.role == "primary" and primary.user.id == manager.user.id
    guest_key, guest_id = enroll(service, manager)
    guest, _ = login(service, guest_key, guest_id)
    assert guest.user.id != manager.user.id and guest.role == "guest"
    assert guest.user.role == "user" and guest.user.permissions == []
    assert service.account(guest)["owner"] is None
    assert service.account(guest)["user"]["id"] == guest.user.id
    assert service.user_for_client(guest_id) == guest.user
    assert {row["role"] for row in service.clients(manager)} == {"primary", "guest"}
    other_key, candidate = primary_candidate(service, management_key, "another-owner")
    other_id = service.prove_pairing(
        candidate["pairing_id"], sign(other_key, candidate["challenge"])
    )["client_id"]
    other, _ = login(service, other_key, other_id)
    assert other.user.id == primary.user.id
    assert set(service.account(other)["owner_client_ids"]) == {primary_id, other_id}
    assert service.account(guest)["owner_client_ids"] == []


@pytest.mark.parametrize(
    "operation", ["share", "clients", "revoke", "primary", "rotate", "browser"]
)
def test_guest_cannot_manage_box(service, management_key, operation):
    manager = admin(service, management_key)
    key, client_id = enroll(service, manager)
    guest, _ = login(service, key, client_id)
    calls = {
        "share": lambda: service.create_enroll_grant(guest, request_id="no"),
        "clients": lambda: service.clients(guest),
        "revoke": lambda: service.revoke_client(guest, client_id),
        "primary": lambda: service.add_owner_device(guest, client_id),
        "rotate": lambda: service.rotate_management_key(guest),
        "browser": lambda: service.browser_session(guest),
    }
    with pytest.raises(InhereBoxError) as denied:
        calls[operation]()
    assert denied.value.status == 403


def test_primary_can_share_with_action_proof_but_cannot_manage_key_or_devices(
    service, management_key
):
    key, client_id = claim(service, management_key)
    context, _ = login(service, key, client_id)
    with pytest.raises(InhereBoxError):
        service.create_enroll_grant(context, request_id="one")
    challenge = service.create_challenge(
        client_id=client_id, action="enroll", params={"request_id": "one"}
    )
    grant = service.create_enroll_grant(context, request_id="one", proof=sign(key, challenge))
    new_key, pairing = begin_pairing(service, grant)
    service.prove_pairing(pairing["pairing_id"], sign(new_key, pairing["challenge"]))
    for operation in (
        lambda: service.revoke_client(context, client_id),
        lambda: service.add_owner_device(context, client_id),
        lambda: service.rotate_management_key(context),
    ):
        with pytest.raises(InhereBoxError) as denied:
            operation()
        assert denied.value.status == 403


def test_adding_owner_preserves_other_devices_and_guest_data(service, management_key):
    manager = admin(service, management_key)
    first_key, first_id = claim(service, management_key)
    second_key, second_id = enroll(service, manager)
    other_key, other_id = enroll(service, manager, "other")
    first, first_token = login(service, first_key, first_id)
    second, second_token = login(service, second_key, second_id)
    other, other_token = login(service, other_key, other_id)
    grant = service.create_enroll_grant(
        first,
        request_id="still-valid",
        proof=sign(
            first_key,
            service.create_challenge(
                client_id=first_id, action="enroll", params={"request_id": "still-valid"}
            ),
        ),
    )
    original_ids = {row["client_id"]: row["guest_user_id"] for row in service.clients(manager)}
    service.add_owner_device(manager, second_id)
    with pytest.raises(InhereBoxError):
        service.validate_access_token(second_token)
    assert service.validate_access_token(first_token) == first
    assert service.validate_access_token(other_token) == other
    second_owner, second_owner_token = login(service, second_key, second_id)
    assert second_owner.user.id == manager.user.id != second.user.id
    begin_pairing(service, grant, request_id="still-usable")
    service.add_owner_device(manager, second_id)  # Idempotent; no session revocation.
    assert service.validate_access_token(second_owner_token) == second_owner
    assert original_ids == {
        row["client_id"]: row["guest_user_id"] for row in service.clients(manager)
    }
    service.revoke_client(manager, first_id)
    with pytest.raises(InhereBoxError):
        service.validate_access_token(first_token)
    assert service.validate_access_token(second_owner_token) == second_owner
    assert service.validate_access_token(other_token) == other
    assert service.account(manager)["owner_client_ids"] == [second_id]
    assert service.status()["primary_client_id"] == second_id


def test_admin_can_revoke_last_primary_and_recover_without_any_client(service, management_key):
    manager = admin(service, management_key)
    key, client_id = claim(service, management_key)
    _, token = login(service, key, client_id)
    service.revoke_client(manager, client_id)
    with pytest.raises(InhereBoxError):
        service.validate_access_token(token)
    assert service.status()["primary_client_id"] is None
    recovered = admin(service, management_key)
    candidate_key, candidate = primary_candidate(service, management_key, "replacement")
    result = service.prove_pairing(
        candidate["pairing_id"], sign(candidate_key, candidate["challenge"])
    )
    assert login(service, candidate_key, result["client_id"])[0].user.id == recovered.user.id


@pytest.mark.parametrize("revoke_clients", [False, True])
def test_key_rotation_revokes_web_and_shares_with_optional_native_revoke(
    service, management_key, revoke_clients
):
    manager = admin(service, management_key)
    key, client_id = claim(service, management_key)
    native, token = login(service, key, client_id)
    grant = service.create_enroll_grant(manager, request_id="pending")
    rotated = service.rotate_management_key(manager, revoke_clients=revoke_clients)
    assert rotated["management_key_revision"] == 2
    with pytest.raises(InhereBoxError):
        service.validate_context(manager)
    with pytest.raises(InhereBoxError):
        service.create_admin_session(management_key)
    with pytest.raises(InhereBoxError):
        begin_pairing(service, grant)
    assert admin(service, rotated["management_key"]).user.id == manager.user.id
    if revoke_clients:
        with pytest.raises(InhereBoxError):
            service.validate_access_token(token)
        assert service.status()["primary_client_id"] is None
    else:
        assert service.validate_access_token(token) == native


def test_browser_refresh_one_winner_and_native_session_preserved(service, management_key):
    key, client_id = claim(service, management_key)
    native, token = login(service, key, client_id)
    manager = admin(service, management_key)
    csrf = service.browser_session(manager)["csrf_token"]
    service.require_browser_csrf(manager, csrf)
    for wrong in ("", "0" * 64, "非" * 64):
        with pytest.raises(InhereBoxError):
            service.require_browser_csrf(manager, wrong)

    def attempt(_):
        try:
            return service.refresh_admin_session(manager)
        except InhereBoxError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert sum(result is not None for result in results) == 1
    assert service.validate_access_token(token) == native
    with pytest.raises(InhereBoxError):
        service.validate_context(manager)
    replacement = next(item for item in results if item is not None)
    refreshed = service.validate_access_token(replacement["access_token"])
    with pytest.raises(InhereBoxError):
        service.require_browser_csrf(refreshed, csrf)
    service.logout(refreshed)
    with pytest.raises(InhereBoxError):
        service.validate_context(refreshed)


@pytest.mark.parametrize(
    "attack", ["wrong_key", "typ", "extra_header", "payload", "expired", "alg"]
)
def test_proof_rejects_changed_or_untrusted_material(service, management_key, attack):
    key, client_id = claim(service, management_key)
    challenge = service.create_challenge(client_id=client_id)
    headers = {"typ": PROOF_TYPE}
    if attack == "wrong_key":
        key = keypair()[0]
    elif attack == "typ":
        headers["typ"] = "JWT"
    elif attack == "extra_header":
        headers["jku"] = "https://untrusted.invalid/key"
    elif attack == "payload":
        challenge["payload"]["action"] = "enroll"
    elif attack == "expired":
        with service._services.db.transaction() as conn:
            conn.execute("UPDATE box_auth_challenges SET expires_at=0")
    proof = jwt.encode(
        challenge["payload"],
        b"fake" * 8 if attack == "alg" else key,
        algorithm="HS256" if attack == "alg" else "ES256",
        headers=headers,
    )
    with pytest.raises(InhereBoxError) as denied:
        service.create_session(proof)
    assert denied.value.details["reason"] == "proof_invalid"


def test_challenge_and_primary_claim_have_one_concurrent_winner(service, management_key):
    key, pairing = primary_candidate(service, management_key)
    proof = sign(key, pairing["challenge"])

    def attempt(_):
        try:
            return service.prove_pairing(pairing["pairing_id"], proof)
        except InhereBoxError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert sum(result is not None for result in results) == 1
    result = next(item for item in results if item is not None)
    replay = service.pairing_result(
        pairing["pairing_id"],
        sign(key, service.create_challenge(pairing_id=pairing["pairing_id"], action="result")),
    )
    assert replay == result
    assert len(service._services.user_repo.list()) == 2


@pytest.mark.parametrize("issuer", ["admin", "primary"])
def test_share_remains_redeemable_until_48_hours_and_is_single_use(
    service, management_key, box_clock, issuer
):
    if issuer == "primary":
        key, client_id = claim(service, management_key)
        context, _ = login(service, key, client_id)
        challenge = service.create_challenge(
            client_id=client_id, action="enroll", params={"request_id": "long-share"}
        )
        proof = sign(key, challenge)
    else:
        context = admin(service, management_key)
        proof = None
    issued_at = box_clock[0]
    grant = service.create_enroll_grant(context, request_id="long-share", proof=proof)
    assert grant["expires_at"] == issued_at + 172800

    box_clock[0] = issued_at + 172799
    guest_key, pairing = begin_pairing(service, grant)
    assert pairing["challenge"]["expires_at"] == box_clock[0] + 60
    result = service.prove_pairing(pairing["pairing_id"], sign(guest_key, pairing["challenge"]))
    assert result["state"] == "consumed" and result["client_id"]
    with pytest.raises(InhereBoxError) as replay:
        begin_pairing(service, grant, request_id="another-device")
    assert replay.value.details["reason"] == "pairing_invalid"


@pytest.mark.parametrize("candidate_bound", [False, True])
def test_share_is_rejected_at_exactly_48_hours(service, management_key, box_clock, candidate_bound):
    grant = service.create_enroll_grant(admin(service, management_key), request_id="expires")
    box_clock[0] = grant["expires_at"] - 1
    if candidate_bound:
        key, pairing = begin_pairing(service, grant)
    box_clock[0] = grant["expires_at"]
    with pytest.raises(InhereBoxError) as expired:
        if candidate_bound:
            # The fresh proof is still valid, but the enclosing share has expired.
            service.prove_pairing(pairing["pairing_id"], sign(key, pairing["challenge"]))
        else:
            begin_pairing(service, grant)
    assert expired.value.details["reason"] == "pairing_invalid"


@pytest.mark.parametrize("elapsed", [299, 300])
def test_primary_claim_keeps_five_minute_window(service, management_key, box_clock, elapsed):
    issued_at = box_clock[0]
    key, pairing = primary_candidate(service, management_key)
    assert pairing["challenge"]["expires_at"] == issued_at + 60
    with service._services.db.connect() as conn:
        assert service._repo.grant(conn, pairing["pairing_id"])["expires_at"] == issued_at + 300

    box_clock[0] = issued_at + 299
    retry = service.create_primary_claim(
        management_key=management_key,
        request_id="primary",
        public_key_jwk=json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key())),
        name="Primary",
        platform="native",
    )
    assert retry["pairing_id"] == pairing["pairing_id"]
    box_clock[0] = issued_at + elapsed
    proof = sign(key, retry["challenge"])
    if elapsed == 299:
        assert service.prove_pairing(pairing["pairing_id"], proof)["state"] == "consumed"
    else:
        with pytest.raises(InhereBoxError) as expired:
            service.prove_pairing(pairing["pairing_id"], proof)
        assert expired.value.details["reason"] == "pairing_invalid"


def test_existing_share_expiry_is_not_extended_on_restart_or_retry(
    service, management_key, box_clock
):
    issued_at = box_clock[0]
    grant = service.create_enroll_grant(admin(service, management_key), request_id="old-share")
    old_expiry = issued_at + 300
    with service._services.db.transaction() as conn:
        # Simulate a persisted share issued before the default changed to 48 hours.
        conn.execute(
            "UPDATE box_pairing_grants SET expires_at=? WHERE grant_id=?",
            (old_expiry, grant["grant_id"]),
        )
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    new_grant = restarted.create_enroll_grant(
        admin(restarted, management_key), request_id="new-share"
    )
    assert new_grant["expires_at"] == issued_at + 172800

    box_clock[0] = old_expiry - 1
    key, pairing = begin_pairing(restarted, grant)
    with restarted._services.db.connect() as conn:
        assert restarted._repo.grant(conn, grant["grant_id"])["expires_at"] == old_expiry
    box_clock[0] = old_expiry
    with pytest.raises(InhereBoxError) as expired:
        restarted.prove_pairing(pairing["pairing_id"], sign(key, pairing["challenge"]))
    assert expired.value.details["reason"] == "pairing_invalid"


@pytest.mark.parametrize("explicit_id", [False, True])
def test_share_secret_only_and_explicit_id_are_idempotent_and_key_bound(
    service, management_key, explicit_id
):
    manager = admin(service, management_key)
    grant = service.create_enroll_grant(manager, request_id="one")
    key, pairing = begin_pairing(service, grant, explicit_id=explicit_id)
    repeated = service.create_pairing(
        **({"grant_id": grant["grant_id"]} if explicit_id else {}),
        secret=grant["secret"],
        request_id="guest",
        public_key_jwk=json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key())),
        name="Guest",
        platform="native",
    )
    assert repeated["pairing_id"] == pairing["pairing_id"]
    with pytest.raises(InhereBoxError):
        begin_pairing(service, grant)
    result = service.prove_pairing(pairing["pairing_id"], sign(key, pairing["challenge"]))
    assert result["state"] == "consumed"
    with pytest.raises(InhereBoxError):
        begin_pairing(service, grant, request_id="reused")
    with pytest.raises(InhereBoxError) as duplicate:
        service.create_enroll_grant(manager, request_id="one")
    assert duplicate.value.status == 409


@pytest.mark.parametrize("invalid", ["wrong", "expired", "cancelled", "failures", "wrong_id"])
def test_invalid_share_does_not_reveal_or_register_any_client(service, management_key, invalid):
    manager = admin(service, management_key)
    grant = service.create_enroll_grant(manager, request_id="one")
    kwargs = {}
    if invalid == "wrong":
        grant["secret"] = "incorrect"
    elif invalid == "wrong_id":
        grant["grant_id"] = "missing"
        kwargs["explicit_id"] = True
    elif invalid == "cancelled":
        service.cancel_pairing(manager, grant["grant_id"])
    elif invalid == "expired":
        with service._services.db.transaction() as conn:
            conn.execute("UPDATE box_pairing_grants SET expires_at=0")
    else:
        for _ in range(10):
            with pytest.raises(InhereBoxError):
                begin_pairing(service, {**grant, "secret": "wrong"}, explicit_id=True)
    with pytest.raises(InhereBoxError) as denied:
        begin_pairing(service, grant, **kwargs)
    assert denied.value.details["reason"] == "pairing_invalid"
    assert service.clients(manager) == []


@pytest.mark.parametrize("completed", [False, True])
def test_expired_pairing_result_preserves_completed_authorization(
    service, management_key, completed
):
    manager = admin(service, management_key)
    grant = service.create_enroll_grant(manager, request_id="one")
    key, pairing = begin_pairing(service, grant)
    if completed:
        service.prove_pairing(pairing["pairing_id"], sign(key, pairing["challenge"]))
    with service._services.db.transaction() as conn:
        conn.execute("UPDATE box_pairing_grants SET expires_at=0")
    result = service.pairing_result(
        pairing["pairing_id"],
        sign(key, service.create_challenge(pairing_id=pairing["pairing_id"], action="result")),
    )
    assert result["state"] == ("consumed" if completed else "expired")
    assert bool(result["client_id"]) == completed


def test_restart_invalidates_sessions_and_pending_proofs_only(service, management_key):
    key, client_id = claim(service, management_key)
    _, token = login(service, key, client_id)
    manager = admin(service, management_key)
    pending = sign(key, service.create_challenge(client_id=client_id))
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    with pytest.raises(InhereBoxError):
        restarted.validate_access_token(token)
    with pytest.raises(InhereBoxError):
        restarted.validate_context(manager)
    with pytest.raises(InhereBoxError):
        restarted.create_session(pending)
    assert login(restarted, key, client_id)[0].user.id == manager.user.id
    assert admin(restarted, management_key).user.id == manager.user.id


@pytest.mark.parametrize("mutation", ["revoke", "rotate", "transfer", "logout"])
def test_old_database_never_revives_authorization(service, management_key, tmp_path, mutation):
    manager = admin(service, management_key)
    key, client_id = claim(service, management_key)
    guest_key, guest_id = enroll(service, manager)
    context, _ = login(service, key, client_id)
    backup = sqlite3.connect(tmp_path / "backup.db")
    with service._services.db.connect() as conn:
        conn.backup(backup)
    if mutation == "revoke":
        service.revoke_client(manager, client_id)
    elif mutation == "rotate":
        service.rotate_management_key(manager)
    elif mutation == "transfer":
        service.add_owner_device(manager, guest_id)
    else:
        service.logout(context)
    with service._services.db.connect() as conn:
        backup.backup(conn)
    backup.close()
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    assert restarted.status()["state"] == "recovery_required"
    with pytest.raises(InhereBoxError):
        restarted.create_admin_session(management_key)
    with pytest.raises(InhereBoxError):
        login(restarted, guest_key, guest_id)


@pytest.mark.parametrize("failure", ["journal", "missing", "corrupt", "wrong_box", "guest_role"])
def test_trust_failures_require_local_recovery(service, management_key, failure):
    manager = admin(service, management_key)
    _, client_id = enroll(service, manager)
    if failure == "journal":
        service._trust.journal.write_text("{}")
    elif failure == "missing":
        service._trust.path.unlink()
    elif failure == "corrupt":
        service._trust.path.write_text("not json")
    elif failure == "guest_role":
        with service._services.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET role='admin' WHERE id=(SELECT guest_user_id FROM box_paired_clients WHERE client_id=?)",
                (client_id,),
            )
    restarted = BoxAccountService(
        service._services,
        secret=b"test-secret" * 4,
        box_id="other" if failure == "wrong_box" else "test-box",
    )
    restarted.initialize()
    assert restarted.status()["state"] == "recovery_required"
    with pytest.raises(InhereBoxError):
        restarted.initialize_management_key(upgrade_legacy=True)


@pytest.mark.parametrize("phase", ["stage", "publish"])
def test_failed_durable_change_fences_running_and_restarted_service(
    service, management_key, monkeypatch, phase
):
    manager = admin(service, management_key)
    original = getattr(service._trust, phase)

    def fail(*args):
        if phase == "stage":
            original(*args)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(service._trust, phase, fail)
    with pytest.raises(OSError):
        service.rotate_management_key(manager)
    assert service.status()["state"] == "recovery_required"
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    assert restarted.status()["state"] == "recovery_required"


def test_admission_validation_uses_existing_transaction_and_rechecks_revocation(
    service, management_key
):
    key, client_id = claim(service, management_key)
    context, _ = login(service, key, client_id)
    with service._services.db.transaction() as conn:
        assert service.validate_context(context, conn) == context
    service.logout(context)
    with service._services.db.transaction() as conn, pytest.raises(InhereBoxError):
        service.validate_context(context, conn)


def test_existing_unowned_users_cannot_be_adopted(service):
    service._services.user_repo.create(username="existing", password_hash="hash", role="admin")
    assert service.status()["state"] == "recovery_required"
    with pytest.raises(InhereBoxError):
        service.initialize_management_key(upgrade_legacy=True)


def test_rate_limit_and_unknown_challenges(service, management_key):
    for _ in range(30):
        service.rate_limit("source")
    with pytest.raises(InhereBoxError) as denied:
        service.rate_limit("source")
    assert denied.value.status == 429
    service.rate_limit("other")
    fake = service.create_challenge(client_id="unknown")
    with service._services.db.connect() as conn:
        assert service._repo.challenge(conn, fake["challenge_id"]) is None
    with pytest.raises(InhereBoxError):
        service.create_session(sign(keypair()[0], fake))


def _legacy_record(service):
    """Build an actual v61 authorization digest without inventing a v2 primary."""
    with service._services.db.transaction() as conn:
        owner_id = service._repo.create_owner(conn, 1)
        conn.execute(
            "INSERT INTO box_paired_clients(client_id,owner_user_id,name,platform,public_key_json,public_key_fingerprint,state,created_at) VALUES('legacy',?,'old browser','web','{}','old','active',1)",
            (owner_id,),
        )
        conn.execute(
            "INSERT INTO box_auth_sessions(session_id,client_id,owner_user_id,auth_revision,boot_id,created_at,expires_at) VALUES('old-session','legacy',?,1,'old-boot',1,9999999999)",
            (owner_id,),
        )
        conn.execute("UPDATE box_account SET auth_model_version=1,auth_revision=1")
        record = service._record(conn, legacy=True)
        service._repo.save_trust(conn, revision=1, digest=record["digest"])
        before = dict(conn.execute("SELECT * FROM users WHERE id=?", (owner_id,)).fetchone())
    service._trust.path.write_text(canonical(record))
    return before


@pytest.mark.parametrize("corrupt", [False, True])
def test_legacy_upgrade_explicit_preserves_owner_and_revokes_every_old_identity(service, corrupt):
    before = _legacy_record(service)
    service.initialize()
    assert service.status()["state"] == "recovery_required"
    with pytest.raises(InhereBoxError) as denied:
        service.initialize_management_key()
    assert denied.value.details["reason"] == "legacy_upgrade_required"
    if corrupt:
        service._trust.path.write_text("{}")
        with pytest.raises(InhereBoxError):
            service.initialize_management_key(upgrade_legacy=True)
        return
    result = service.initialize_management_key(upgrade_legacy=True)
    manager = admin(service, result["management_key"])
    assert manager.user.id == before["id"] and service.account(manager)["primary_client_id"] is None
    with service._services.db.connect() as conn:
        assert (
            dict(conn.execute("SELECT * FROM users WHERE id=?", (before["id"],)).fetchone())
            == before
        )
        assert service._repo.client(conn, "legacy")["state"] == "revoked"
        assert service._repo.session(conn, "old-session")["revoked_at"] is not None
        assert service._repo.account(conn)["auth_model_version"] == 2
    key, client_id = claim(service, result["management_key"])
    assert login(service, key, client_id)[0].user.id == before["id"]


def test_migration_062_preserves_old_rows_and_dialect_parity(tmp_path, monkeypatch):
    old = tmp_path / "v61"
    old.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 61:
            shutil.copy2(source, old / source.name)
    db = SqlitePool(tmp_path / "upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old)
            run_migrations(db)
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('keep','hash','admin',1)"
            )
            before = dict(conn.execute("SELECT * FROM users").fetchone())
            conn.execute(
                "INSERT INTO box_account(singleton,box_id,owner_user_id) VALUES(1,'legacy',?)",
                (before["id"],),
            )
            conn.execute(
                "INSERT INTO box_paired_clients(client_id,owner_user_id,name,platform,public_key_json,public_key_fingerprint,state,created_at) VALUES('legacy',?,'browser','web','{}','old','active',1)",
                (before["id"],),
            )
        run_migrations(db)
        run_migrations(db)
        with db.connect() as conn:
            assert dict(conn.execute("SELECT * FROM users").fetchone()) == before
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 63
            assert conn.execute("SELECT auth_model_version FROM box_account").fetchone()[0] == 1
            assert (
                conn.execute("SELECT role,guest_user_id FROM box_paired_clients").fetchone()["role"]
                == "legacy"
            )
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
    sqlite_sql = (migrate._MIGRATIONS_DIR / "062_box_management_identity.sql").read_text()
    postgres_sql = (migrate._MIGRATIONS_DIR / "062_box_management_identity.pg.sql").read_text()
    assert postgres_sql == sqlite_sql.replace(
        "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
    ).replace("user_id INTEGER", "user_id BIGINT").replace(
        " ADD COLUMN ", " ADD COLUMN IF NOT EXISTS "
    )


def test_management_migration_replay_preserves_live_authorization(service, management_key):
    manager = admin(service, management_key)
    key, client_id = claim(service, management_key)
    client, token = login(service, key, client_id)
    with service._services.db.transaction() as conn:
        account = service._repo.account(conn)
        clients = service._repo.clients(conn)
        conn.execute("UPDATE _schema_version SET version=61")
    trust = service._trust.read()

    run_migrations(service._services.db)
    run_migrations(service._services.db)

    with service._services.db.connect() as conn:
        assert service._repo.account(conn) == account
        assert service._repo.clients(conn) == clients
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 63
    assert service._trust.read() == trust
    assert service.validate_context(manager) == manager
    assert service.validate_access_token(token) == client


def test_v62_upgrade_preserves_owner_and_trust_then_allows_multiple_devices(
    service, management_key
):
    key, first_id = claim(service, management_key)
    first, token = login(service, key, first_id)
    with service._services.db.transaction() as conn:
        conn.execute(
            "CREATE UNIQUE INDEX idx_box_active_primary ON box_paired_clients(role) WHERE role='primary' AND state='active'"
        )
        conn.execute("UPDATE _schema_version SET version=62")
        before = service._repo.account(conn)
    trust = service._trust.read()
    run_migrations(service._services.db)
    with service._services.db.connect() as conn:
        assert service._repo.account(conn) == before
    assert service._trust.read() == trust
    assert service.validate_access_token(token) == first
    other_key, candidate = primary_candidate(service, management_key, "upgraded-owner")
    other_id = service.prove_pairing(
        candidate["pairing_id"], sign(other_key, candidate["challenge"])
    )["client_id"]
    assert login(service, other_key, other_id)[0].user.id == first.user.id
    # Repairing an old watermark must not recreate the removed singleton index.
    with service._services.db.transaction() as conn:
        conn.execute("UPDATE _schema_version SET version=61")
    run_migrations(service._services.db)
    run_migrations(service._services.db)
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    assert login(restarted, key, first_id)[0].user.id == first.user.id
    assert login(restarted, other_key, other_id)[0].user.id == first.user.id
    sqlite_sql = (migrate._MIGRATIONS_DIR / "063_multiple_owner_devices.sql").read_text()
    assert sqlite_sql == (migrate._MIGRATIONS_DIR / "063_multiple_owner_devices.pg.sql").read_text()


def test_independent_owner_candidates_survive_another_registration(service, management_key):
    first_key, first = primary_candidate(service, management_key, "first")
    second_key, second = primary_candidate(service, management_key, "second")
    first_id = service.prove_pairing(first["pairing_id"], sign(first_key, first["challenge"]))[
        "client_id"
    ]
    # A changed authorization revision invalidates the old challenge, not the candidate.
    with pytest.raises(InhereBoxError):
        service.prove_pairing(second["pairing_id"], sign(second_key, second["challenge"]))
    retry = service.create_primary_claim(
        management_key=management_key,
        request_id="second",
        public_key_jwk=json.loads(jwt.algorithms.ECAlgorithm.to_jwk(second_key.public_key())),
        name="Primary",
        platform="native",
    )
    assert retry["pairing_id"] == second["pairing_id"]
    second_id = service.prove_pairing(retry["pairing_id"], sign(second_key, retry["challenge"]))[
        "client_id"
    ]
    assert (
        login(service, first_key, first_id)[0].user.id
        == login(service, second_key, second_id)[0].user.id
    )


@pytest.mark.parametrize("role", ["primary", "guest"])
def test_delete_revoked_registration_preserves_business_data(service, management_key, role):
    from inhere_box.infra.db.repos.project_tasks import ProjectTaskRow
    from inhere_box.infra.db.repos.projects import ProjectRow

    manager = admin(service, management_key)
    key, client_id = (
        claim(service, management_key) if role == "primary" else enroll(service, manager)
    )
    context, token = login(service, key, client_id)
    project = ProjectRow(
        project_id="kept-project",
        owner_id=context.user.id,
        name="Keep",
        description="",
        directory=None,
        client_directory=None,
        instructions="",
        skills_json="[]",
        mcp_servers_json="[]",
        revision=1,
        created_at=1,
        updated_at=1,
    )
    task = ProjectTaskRow(
        task_id="kept-task",
        owner_id=context.user.id,
        project_id=project.project_id,
        title="Keep task",
        request_id="kept-request",
        active_turn_id=None,
        created_at=1,
        updated_at=1,
    )
    with service._services.db.transaction() as conn:
        service._services.project_repo.create(conn, project)
        service._services.project_task_repo.insert(conn, task)
    other_key, other_id = enroll(service, manager, request_id="other-client")
    other, other_token = login(service, other_key, other_id)
    with pytest.raises(InhereBoxError) as denied:
        service.delete_client(context, client_id)
    assert denied.value.status == 403
    with pytest.raises(InhereBoxError) as active:
        service.delete_client(manager, client_id)
    assert active.value.status == 409
    service.revoke_client(manager, client_id)
    service.create_challenge(client_id=client_id, action="client-status")
    service.delete_client(manager, client_id)
    assert [client["client_id"] for client in service.clients(manager)] == [other_id]
    assert service.validate_access_token(other_token) == other
    with pytest.raises(InhereBoxError):
        service.validate_access_token(token)
    with pytest.raises(InhereBoxError):
        manual_login(service, key, client_id, management_key)
    with pytest.raises(InhereBoxError) as missing:
        service.delete_client(manager, client_id)
    assert missing.value.status == 404
    with service._services.db.connect() as conn:
        assert service._repo.owner(conn, context.user.id) is not None
        assert (
            conn.execute(
                "SELECT name FROM projects WHERE project_id=?", (project.project_id,)
            ).fetchone()[0]
            == "Keep"
        )
        assert (
            conn.execute(
                "SELECT title FROM project_tasks WHERE task_id=?", (task.task_id,)
            ).fetchone()[0]
            == "Keep task"
        )
        for table in ["box_auth_challenges", "box_auth_sessions", "box_pairing_grants"]:
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE client_id=?", (client_id,)
                ).fetchone()[0]
                == 0
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    assert login(restarted, other_key, other_id)[0].user == other.user
    assert restarted.status()["state"] == "claimed"


def test_delete_revoked_issuer_preserves_other_registered_clients(service, management_key):
    manager = admin(service, management_key)
    key, client_id = claim(service, management_key)
    owner, _ = login(service, key, client_id)
    grant = service.create_enroll_grant(
        owner,
        request_id="issued",
        proof=sign(
            key,
            service.create_challenge(
                client_id=client_id, action="enroll", params={"request_id": "issued"}
            ),
        ),
    )
    guest_key, pairing = begin_pairing(service, grant)
    guest_id = service.prove_pairing(pairing["pairing_id"], sign(guest_key, pairing["challenge"]))[
        "client_id"
    ]
    service.create_challenge(pairing_id=pairing["pairing_id"], action="result")
    service.revoke_client(manager, client_id)
    service.delete_client(manager, client_id)
    assert login(service, guest_key, guest_id)[0].role == "guest"
    with service._services.db.connect() as conn:
        assert service._repo.grant(conn, grant["grant_id"]) is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def manual_login(service, key, client_id, secret):
    from inhere_box.infra.auth.box_accounts.crypto import digest

    challenge = service.create_challenge(
        client_id=client_id, action="login", params={"secret_hash": digest(secret)}
    )
    return service.login_client(sign(key, challenge), secret)


def test_manual_owner_login_requires_current_input_and_keeps_device(service, management_key):
    key, client_id = claim(service, management_key)
    context = admin(service, management_key)
    before = service.account(login(service, key, client_id)[0])
    for secret in ["wrong", ""]:
        with pytest.raises(InhereBoxError):
            manual_login(service, key, client_id, secret)
    for _ in range(2):
        session = manual_login(service, key, client_id, management_key)
        verified = service.validate_access_token(session["access_token"])
        assert session["paired_client_id"] == client_id
        assert service.account(verified)["user"]["id"] == before["user"]["id"]
    assert len(service.clients(context)) == 1


def test_manual_login_binds_input_and_is_not_a_renewal_proof(service, management_key):
    from inhere_box.infra.auth.box_accounts.crypto import digest

    key, client_id = claim(service, management_key)
    challenge = service.create_challenge(
        client_id=client_id, action="login", params={"secret_hash": digest(management_key)}
    )
    proof = sign(key, challenge)
    with pytest.raises(InhereBoxError):
        service.login_client(proof, "different-input")
    with pytest.raises(InhereBoxError):
        service.create_session(proof)
    result = service.login_client(proof, management_key)
    assert result["paired_client_id"] == client_id
    with pytest.raises(InhereBoxError):
        service.login_client(proof, management_key)
    rotated = service.rotate_management_key_local()["management_key"]
    with pytest.raises(InhereBoxError):
        manual_login(service, key, client_id, management_key)
    assert manual_login(service, key, client_id, rotated)["paired_client_id"] == client_id


def test_manual_guest_login_consumes_fresh_share_and_preserves_workspace(service, management_key):
    context = admin(service, management_key)
    key, client_id = enroll(service, context)
    old_context = login(service, key, client_id)[0]
    grant = service.create_enroll_grant(context, request_id="reconnect-share")
    session = manual_login(service, key, client_id, grant["secret"])
    verified = service.validate_access_token(session["access_token"])
    assert session["paired_client_id"] == client_id and session["role"] == "guest"
    assert verified.user.id == old_context.user.id
    assert service.account(verified)["user"]["id"] == service.account(old_context)["user"]["id"]
    assert len(service.clients(context)) == 1
    with pytest.raises(InhereBoxError):
        manual_login(service, key, client_id, grant["secret"])
    with pytest.raises(InhereBoxError):
        manual_login(service, key, client_id, management_key)


def test_manual_guest_login_cannot_consume_another_candidate(service, management_key):
    context = admin(service, management_key)
    key, client_id = enroll(service, context)
    grant = service.create_enroll_grant(context, request_id="bound-share")
    other_key, pairing = begin_pairing(service, grant, request_id="bound-candidate")
    with pytest.raises(InhereBoxError):
        manual_login(service, key, client_id, grant["secret"])
    assert (
        service.prove_pairing(pairing["pairing_id"], sign(other_key, pairing["challenge"]))["state"]
        == "consumed"
    )


def test_revoked_device_status_does_not_restore_access(service, management_key):
    key, client_id = claim(service, management_key)
    context = admin(service, management_key)
    for expected in ["active", "revoked"]:
        if expected == "revoked":
            service.revoke_client(context, client_id)
        challenge = service.create_challenge(client_id=client_id, action="client-status")
        proof = sign(key, challenge)
        with pytest.raises(InhereBoxError):
            service.client_status("other-client", proof)
        status = service.client_status(client_id, proof)
        assert status["box_id"] == service.box_id and status["client_id"] == client_id
        assert status["state"] == expected
        assert (status["revoked_at"] is not None) == (expected == "revoked")
        with pytest.raises(InhereBoxError):
            service.client_status(client_id, proof)
    with pytest.raises(InhereBoxError):
        login(service, key, client_id)


@pytest.mark.parametrize("role", ["primary", "guest"])
def test_manual_login_restores_revoked_device_but_not_old_authorization(
    service, management_key, role
):
    context = admin(service, management_key)
    key, client_id = (
        claim(service, management_key) if role == "primary" else enroll(service, context)
    )
    before, token = login(service, key, client_id)
    old_proof = sign(key, service.create_challenge(client_id=client_id))
    service.revoke_client(context, client_id)
    for invalid in ["wrong", management_key] if role == "guest" else ["wrong"]:
        with pytest.raises(InhereBoxError):
            manual_login(service, key, client_id, invalid)
    with service._services.db.connect() as conn:
        assert service._repo.client(conn, client_id)["state"] == "revoked"
    with pytest.raises(InhereBoxError):
        login(service, key, client_id)
    secret = management_key
    if role == "guest":
        secret = service.create_enroll_grant(context, request_id="restore-share")["secret"]
    session = manual_login(service, key, client_id, secret)
    restored = service.validate_access_token(session["access_token"])
    assert restored.user == before.user and restored.role == role
    assert session["paired_client_id"] == client_id
    clients = service.clients(context)
    assert len(clients) == 1
    assert clients[0]["state"] == "active" and clients[0]["revoked_at"] is None
    for operation in [
        lambda: service.validate_access_token(token),
        lambda: service.validate_context(before),
        lambda: service.create_session(old_proof),
    ]:
        with pytest.raises(InhereBoxError):
            operation()
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    assert login(restarted, key, client_id)[0].user == before.user
    if role == "guest":
        with pytest.raises(InhereBoxError):
            manual_login(restarted, key, client_id, secret)


@pytest.mark.parametrize("invalid", ["expired", "consumed", "cancelled", "issuer_revoked"])
def test_invalid_share_cannot_restore_revoked_guest(service, management_key, box_clock, invalid):
    context = admin(service, management_key)
    owner_key, owner_id = claim(service, management_key)
    owner, _ = login(service, owner_key, owner_id)
    key, client_id = enroll(service, context)
    grant = service.create_enroll_grant(
        owner,
        request_id="restore",
        proof=sign(
            owner_key,
            service.create_challenge(
                client_id=owner_id, action="enroll", params={"request_id": "restore"}
            ),
        ),
    )
    if invalid == "consumed":
        manual_login(service, key, client_id, grant["secret"])
    service.revoke_client(context, client_id)
    if invalid == "expired":
        box_clock[0] = grant["expires_at"]
    elif invalid == "cancelled":
        service.cancel_pairing(context, grant["grant_id"])
    elif invalid == "issuer_revoked":
        service.revoke_client(context, owner_id)
    with pytest.raises(InhereBoxError):
        manual_login(service, key, client_id, grant["secret"])
    with service._services.db.connect() as conn:
        client = service._repo.client(conn, client_id)
        assert client["state"] == "revoked" and client["revoked_at"] is not None
    assert service.status()["state"] == "claimed"
