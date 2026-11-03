"""Legacy account entrypoints cannot bypass the box's owner and pairing lifecycle."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inhere_box.cli.main import cli
from inhere_box.cli.support import offline_ops
from inhere_box.config import InhereBoxConfig
from inhere_box.infra.auth.ldap.client import LdapIdentity
from inhere_box.infra.auth.ldap.service import LdapAuthService
from inhere_box.infra.db.migrate import run_migrations
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.services import SharedServices, build_shared_services
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.users.identity import User
from inhere_box.infra.users.invites import InviteService
from inhere_box.infra.users.manager import UserManager
from inhere_box.infra.utils.paths import PathLayout


@pytest.fixture
def box_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SharedServices]:
    monkeypatch.setenv("INHERE_BOX_HOME", str(tmp_path))
    paths = PathLayout(tmp_path)
    paths.config.write_text(json.dumps({"account_mode": "box_local"}), encoding="utf-8")
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(
        db=db, paths=paths, config=InhereBoxConfig(account_mode="box_local")
    )
    # The claim transaction owns creation. Seed its resulting owner row without
    # going through the legacy manager or introducing real workspace I/O.
    services.user_repo.create(username="owner", password_hash=None, role="admin")
    try:
        yield services
    finally:
        db.close()


def assert_mode_rejection(exc: InhereBoxError) -> None:
    assert exc.code is ErrorCode.FORBIDDEN
    assert exc.status == 403
    assert exc.details == {"reason": "box_account_mode"}


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("create", (), {"username": "second", "password": "TestPass12", "role": "admin"}),
        (
            "create_from_invite",
            (),
            {"code": "pending", "username": "second", "password": "TestPass12"},
        ),
        (
            "resolve_or_create_sso_user",
            (),
            {"provider_id": 1, "subject": "sso-owner", "claims": {}},
        ),
        (
            "bind_sso_identity",
            (),
            {"user_id": 1, "provider_id": 1, "subject": "sso-owner", "claims": {}},
        ),
        ("unbind_sso_identity", (), {"user_id": 1, "kind": "oidc"}),
        ("authenticate", ("owner", "TestPass12"), {}),
        ("change_password", ("owner", "TestPass12", "NewPass34"), {}),
        ("reset_password", ("owner", "NewPass34"), {}),
        ("set_permissions", ("owner", []), {}),
        ("set_resource_policy", ("owner",), {"token_quota": 1}),
        ("set_role", ("owner", "user"), {}),
        ("disable", ("owner",), {}),
        ("enable", ("owner",), {}),
        ("unlock_login", ("owner",), {}),
        ("remove", ("owner",), {}),
    ],
)
async def test_legacy_manager_account_operations_are_blocked(
    box_services: SharedServices, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    manager = UserManager(box_services)
    before = box_services.user_repo.list(include_disabled=True)
    owner = before[0]
    manager.register_cached_user(
        User(id=owner.id, username=owner.username, role=owner.role, display_name=None)
    )
    with pytest.raises(InhereBoxError) as exc:
        await getattr(manager, method)(*args, **kwargs)
    assert_mode_rejection(exc.value)
    assert box_services.user_repo.list(include_disabled=True) == before
    assert manager.get_by_id(owner.id) is not None
    assert box_services.resident_conversation_repo.get(owner.id) is None


async def test_owner_profile_and_preferences_remain_editable(box_services: SharedServices) -> None:
    manager = UserManager(box_services)
    await manager.set_display_name("owner", "My name")
    await manager.set_email("owner", "owner@example.com")
    await manager.set_locale("owner", "zh")
    await manager.set_remote_browser_bookmarks("owner", [])
    await manager.set_model_preferences("owner", preferred_model="provider/model")
    row = box_services.user_repo.get_by_username("owner")
    assert row is not None
    assert row.display_name == "My name"
    assert row.email == "owner@example.com"
    assert row.locale == "zh"
    assert json.loads(row.preferences_json)["preferred_model"] == "provider/model"
    assert row.password_hash is None
    assert row.role == "admin"
    assert not row.disabled


def test_invites_cannot_create_or_advertise_legacy_signup(box_services: SharedServices) -> None:
    owner = box_services.user_repo.get_by_username("owner")
    assert owner is not None
    invite = box_services.invite_repo.create(
        code="old-invite", created_by=owner.id, expires_at=2**31
    )
    service = InviteService(box_services)
    with pytest.raises(InhereBoxError) as exc:
        service.create(created_by=owner.id, actor_username="owner")
    assert_mode_rejection(exc.value)
    with pytest.raises(InhereBoxError) as exc:
        service.validate(invite.code)
    assert_mode_rejection(exc.value)
    registered: list[User] = []
    with pytest.raises(InhereBoxError) as exc:
        service.redeem(
            code=invite.code,
            username="second",
            password="TestPass12",
            display_name=None,
            locale=None,
            register_user=registered.append,
        )
    assert_mode_rejection(exc.value)
    assert not registered
    assert box_services.user_repo.count() == 1
    assert box_services.invite_repo.get(invite.id) == invite


async def test_ldap_neither_authenticates_nor_provisions(box_services: SharedServices) -> None:
    service = LdapAuthService(box_services, UserManager(box_services))
    with pytest.raises(InhereBoxError) as exc:
        service.directory_authenticate("owner", "TestPass12")
    assert_mode_rejection(exc.value)
    with pytest.raises(InhereBoxError) as exc:
        await service.resolve_user(
            LdapIdentity(
                dn="uid=second,dc=example,dc=com",
                username="second",
                email=None,
                display_name=None,
                groups=(),
                subject="directory-subject",
            )
        )
    assert_mode_rejection(exc.value)
    assert box_services.user_repo.count() == 1


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        (
            "create_user_offline",
            (),
            {"username": "second", "password": "TestPass12", "role": "admin"},
        ),
        ("set_user_password_offline", ("owner", "TestPass12"), {}),
        ("set_user_role_offline", ("owner", "user"), {}),
        ("disable_user_offline", ("owner",), {}),
        ("delete_user_offline", ("owner",), {}),
    ],
)
def test_offline_cli_cannot_bypass_box_account_mode(
    box_services: SharedServices, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    before = box_services.user_repo.list(include_disabled=True)
    with pytest.raises(InhereBoxError) as exc:
        getattr(offline_ops, method)(*args, **kwargs, home=box_services.paths.root)
    assert_mode_rejection(exc.value)
    assert box_services.user_repo.list(include_disabled=True) == before


@pytest.mark.parametrize("force", [False, True])
def test_init_cannot_replace_claim_or_wipe_box_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    monkeypatch.setenv("INHERE_BOX_HOME", str(tmp_path))
    config_path = tmp_path / "config.json"
    config_text = json.dumps({"account_mode": "box_local"})
    config_path.write_text(config_text, encoding="utf-8")
    marker = tmp_path / "box-identity"
    marker.write_text("keep", encoding="utf-8")
    arguments = [
        "init",
        "--yes",
        "--admin-username",
        "second",
        "--admin-password",
        "TestPass12",
    ]
    if force:
        arguments.append("--force")
    result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 1
    assert "error:" in result.output
    assert config_path.read_text(encoding="utf-8") == config_text
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "inhere-box.db").exists()
