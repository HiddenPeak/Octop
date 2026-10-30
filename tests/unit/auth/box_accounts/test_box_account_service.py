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

from octop.config import OctopConfig
from octop.infra.auth.box_accounts import BoxAccountService
from octop.infra.auth.box_accounts.crypto import PROOF_TYPE, canonical
from octop.infra.db import migrate
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    paths = PathLayout(tmp_path)
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(
        db=db, paths=paths, config=OctopConfig(account_mode="box_local")
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
        "octop.infra.auth.box_accounts.service.time", SimpleNamespace(time=lambda: now[0])
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
    with pytest.raises(OctopError):
        service.initialize_management_key()
    with pytest.raises(OctopError):
        service.create_admin_session("wrong")
    with pytest.raises(OctopError):
        service.create_claim_grant()


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
    with pytest.raises(OctopError):
        primary_candidate(service, management_key, "takeover")


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
        "primary": lambda: service.set_primary(guest, client_id),
        "rotate": lambda: service.rotate_management_key(guest),
        "browser": lambda: service.browser_session(guest),
    }
    with pytest.raises(OctopError) as denied:
        calls[operation]()
    assert denied.value.status == 403


def test_primary_can_share_with_action_proof_but_cannot_manage_key_or_devices(
    service, management_key
):
    key, client_id = claim(service, management_key)
    context, _ = login(service, key, client_id)
    with pytest.raises(OctopError):
        service.create_enroll_grant(context, request_id="one")
    challenge = service.create_challenge(
        client_id=client_id, action="enroll", params={"request_id": "one"}
    )
    grant = service.create_enroll_grant(context, request_id="one", proof=sign(key, challenge))
    new_key, pairing = begin_pairing(service, grant)
    service.prove_pairing(pairing["pairing_id"], sign(new_key, pairing["challenge"]))
    for operation in (
        lambda: service.revoke_client(context, client_id),
        lambda: service.set_primary(context, client_id),
        lambda: service.rotate_management_key(context),
    ):
        with pytest.raises(OctopError) as denied:
            operation()
        assert denied.value.status == 403


def test_primary_transfer_revokes_both_sessions_and_retains_guest_identity(service, management_key):
    manager = admin(service, management_key)
    first_key, first_id = claim(service, management_key)
    second_key, second_id = enroll(service, manager)
    other_key, other_id = enroll(service, manager, "other")
    first, first_token = login(service, first_key, first_id)
    second, second_token = login(service, second_key, second_id)
    other, other_token = login(service, other_key, other_id)
    proof = sign(
        first_key,
        service.create_challenge(
            client_id=first_id, action="enroll", params={"request_id": "cancel"}
        ),
    )
    grant = service.create_enroll_grant(first, request_id="cancel", proof=proof)
    original_ids = {row["client_id"]: row["guest_user_id"] for row in service.clients(manager)}
    service.set_primary(manager, second_id)
    for token in (first_token, second_token):
        with pytest.raises(OctopError):
            service.validate_access_token(token)
    assert service.validate_access_token(other_token) == other
    assert login(service, first_key, first_id)[0].user.id == original_ids[first_id]
    assert login(service, second_key, second_id)[0].user.id == manager.user.id
    with pytest.raises(OctopError):
        begin_pairing(service, grant, request_id="cancelled")
    service.set_primary(manager, first_id)
    assert login(service, second_key, second_id)[0].user.id == second.user.id
    assert original_ids == {
        row["client_id"]: row["guest_user_id"] for row in service.clients(manager)
    }


def test_admin_can_revoke_last_primary_and_recover_without_any_client(service, management_key):
    manager = admin(service, management_key)
    key, client_id = claim(service, management_key)
    _, token = login(service, key, client_id)
    service.revoke_client(manager, client_id)
    with pytest.raises(OctopError):
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
    with pytest.raises(OctopError):
        service.validate_context(manager)
    with pytest.raises(OctopError):
        service.create_admin_session(management_key)
    with pytest.raises(OctopError):
        begin_pairing(service, grant)
    assert admin(service, rotated["management_key"]).user.id == manager.user.id
    if revoke_clients:
        with pytest.raises(OctopError):
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
        with pytest.raises(OctopError):
            service.require_browser_csrf(manager, wrong)

    def attempt(_):
        try:
            return service.refresh_admin_session(manager)
        except OctopError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert sum(result is not None for result in results) == 1
    assert service.validate_access_token(token) == native
    with pytest.raises(OctopError):
        service.validate_context(manager)
    replacement = next(item for item in results if item is not None)
    refreshed = service.validate_access_token(replacement["access_token"])
    with pytest.raises(OctopError):
        service.require_browser_csrf(refreshed, csrf)
    service.logout(refreshed)
    with pytest.raises(OctopError):
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
    with pytest.raises(OctopError) as denied:
        service.create_session(proof)
    assert denied.value.details["reason"] == "proof_invalid"


def test_challenge_and_primary_claim_have_one_concurrent_winner(service, management_key):
    key, pairing = primary_candidate(service, management_key)
    proof = sign(key, pairing["challenge"])

    def attempt(_):
        try:
            return service.prove_pairing(pairing["pairing_id"], proof)
        except OctopError:
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
    with pytest.raises(OctopError) as replay:
        begin_pairing(service, grant, request_id="another-device")
    assert replay.value.details["reason"] == "pairing_invalid"


@pytest.mark.parametrize("candidate_bound", [False, True])
def test_share_is_rejected_at_exactly_48_hours(service, management_key, box_clock, candidate_bound):
    grant = service.create_enroll_grant(admin(service, management_key), request_id="expires")
    box_clock[0] = grant["expires_at"] - 1
    if candidate_bound:
        key, pairing = begin_pairing(service, grant)
    box_clock[0] = grant["expires_at"]
    with pytest.raises(OctopError) as expired:
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
        with pytest.raises(OctopError) as expired:
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
    with pytest.raises(OctopError) as expired:
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
    with pytest.raises(OctopError):
        begin_pairing(service, grant)
    result = service.prove_pairing(pairing["pairing_id"], sign(key, pairing["challenge"]))
    assert result["state"] == "consumed"
    with pytest.raises(OctopError):
        begin_pairing(service, grant, request_id="reused")
    with pytest.raises(OctopError) as duplicate:
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
            with pytest.raises(OctopError):
                begin_pairing(service, {**grant, "secret": "wrong"}, explicit_id=True)
    with pytest.raises(OctopError) as denied:
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
    with pytest.raises(OctopError):
        restarted.validate_access_token(token)
    with pytest.raises(OctopError):
        restarted.validate_context(manager)
    with pytest.raises(OctopError):
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
        service.set_primary(manager, guest_id)
    else:
        service.logout(context)
    with service._services.db.connect() as conn:
        backup.backup(conn)
    backup.close()
    restarted = BoxAccountService(service._services, secret=b"test-secret" * 4, box_id="test-box")
    restarted.initialize()
    assert restarted.status()["state"] == "recovery_required"
    with pytest.raises(OctopError):
        restarted.create_admin_session(management_key)
    with pytest.raises(OctopError):
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
    with pytest.raises(OctopError):
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
    with service._services.db.transaction() as conn, pytest.raises(OctopError):
        service.validate_context(context, conn)


def test_existing_unowned_users_cannot_be_adopted(service):
    service._services.user_repo.create(username="existing", password_hash="hash", role="admin")
    assert service.status()["state"] == "recovery_required"
    with pytest.raises(OctopError):
        service.initialize_management_key(upgrade_legacy=True)


def test_rate_limit_and_unknown_challenges(service, management_key):
    for _ in range(30):
        service.rate_limit("source")
    with pytest.raises(OctopError) as denied:
        service.rate_limit("source")
    assert denied.value.status == 429
    service.rate_limit("other")
    fake = service.create_challenge(client_id="unknown")
    with service._services.db.connect() as conn:
        assert service._repo.challenge(conn, fake["challenge_id"]) is None
    with pytest.raises(OctopError):
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
    with pytest.raises(OctopError) as denied:
        service.initialize_management_key()
    assert denied.value.details["reason"] == "legacy_upgrade_required"
    if corrupt:
        service._trust.path.write_text("{}")
        with pytest.raises(OctopError):
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
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 62
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
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 62
    assert service._trust.read() == trust
    assert service.validate_context(manager) == manager
    assert service.validate_access_token(token) == client
