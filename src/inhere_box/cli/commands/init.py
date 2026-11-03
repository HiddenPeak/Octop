"""`inhere-box init` — bootstrap a fresh inhere-box install (DB + first admin)."""

from __future__ import annotations

import shutil

import click


@click.command("init")
@click.option(
    "--admin-username",
    envvar="INHERE_BOX_ADMIN_USERNAME",
    default=None,
    help="First admin username (or set INHERE_BOX_ADMIN_USERNAME).",
)
@click.option(
    "--admin-password",
    envvar="INHERE_BOX_ADMIN_PASSWORD",
    default=None,
    help="First admin password (or set INHERE_BOX_ADMIN_PASSWORD).",
)
@click.option(
    "--admin-display-name",
    envvar="INHERE_BOX_ADMIN_DISPLAY_NAME",
    default=None,
    help="Optional display name for the admin user.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Wipe existing ~/.inhere-box contents before bootstrapping.",
)
@click.option(
    "--yes",
    "non_interactive",
    is_flag=True,
    default=False,
    help="Skip all interactive prompts.",
)
def init(
    admin_username: str | None,
    admin_password: str | None,
    admin_display_name: str | None,
    force: bool,
    non_interactive: bool,
) -> None:
    """Bootstrap an inhere-box server (~/.inhere-box dir, DB migrations, first admin)."""
    from inhere_box.config import load_config
    from inhere_box.infra.agents.plugins.manager import PluginManager
    from inhere_box.infra.db.factory import open_database
    from inhere_box.infra.db.migrate import run_migrations
    from inhere_box.infra.db.repos.users import UserRepo
    from inhere_box.infra.errors import InhereBoxError
    from inhere_box.infra.users.account_mode import require_server_account_mode
    from inhere_box.infra.users.password import hash_password, validate_password_policy
    from inhere_box.infra.utils.env_file import apply_env_file, env_file_path
    from inhere_box.infra.utils.paths import PathLayout

    paths = PathLayout.from_env()
    home = paths.root

    # Check the mode before --force can delete the box identity or pairing state.
    apply_env_file(env_file_path(paths.root))
    try:
        require_server_account_mode(load_config(paths.config))
    except InhereBoxError as exc:
        from inhere_box.cli.support.errors import fail_octop

        fail_octop(exc)

    # Sidecar files (inhere-box-login.txt, NAS share metadata) must not block first
    # bootstrap. Only an existing control-plane DB counts as an install.
    has_db = (home / "inhere-box.db").is_file()
    if home.exists() and any(home.iterdir()):
        if has_db and not force:
            click.echo(
                f"error: {home} already exists and is not empty. Use --force to reset.",
                err=True,
            )
            raise SystemExit(1)
        if force:
            if not non_interactive:
                from inhere_box.cli.support import prompts as _prompts

                if not _prompts.confirm(
                    f"Wipe {home}? This deletes ALL inhere-box state.", default=False
                ):
                    click.echo("aborted", err=True)
                    raise SystemExit(1)
            shutil.rmtree(home)

    paths.ensure_root()
    PluginManager(plugins_dir=paths.plugins_dir, config_path=paths.config).seed_bundled()
    apply_env_file(env_file_path(paths.root))
    config = load_config(paths.config)
    db = open_database(config, paths)
    try:
        run_migrations(db)

        username = admin_username
        password = admin_password
        display_name = admin_display_name

        if not non_interactive:
            from inhere_box.cli.support import prompts as _prompts

            if not username:
                username = _prompts.text("Admin username:")
            if not password:
                password = _prompts.password("Admin password:")
            if display_name is None:
                display_name = _prompts.text("Display name (optional):", default="") or None

        if not username:
            click.echo("error: admin username is required", err=True)
            raise SystemExit(1)
        try:
            validate_password_policy(password or "")
        except InhereBoxError as exc:
            click.echo(f"error: {exc.message}", err=True)
            raise SystemExit(1) from None

        user_repo = UserRepo(db)
        if user_repo.get_by_username(username) is not None:
            # 幂等引导（issue #1866）：外部数据库（PostgreSQL）不会在 ~/.inhere-box
            # 生成 inhere-box.db，容器重启后 entrypoint 会重跑 init；此时跳过创建，
            # 避免管理员 INSERT 撞 UNIQUE(username) 直接崩溃。
            click.echo(f"  admin user '{username}' already exists, skipping.")
        else:
            user_repo.create(
                username=username,
                password_hash=hash_password(password or ""),
                role="admin",
                display_name=display_name,
            )
    finally:
        db.close()

    click.echo(f"\u2705 inhere-box bootstrapped at {home}")
    click.echo(f"   admin user: {username}")
    click.echo(
        "   next: `inhere-box run` (optional: `inhere-box agent use <id>` to pin default agent)"
    )
