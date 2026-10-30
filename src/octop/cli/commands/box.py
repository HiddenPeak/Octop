"""Trusted host maintenance for a box's independent management credential."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import click

from octop.config import load_config
from octop.i18n import tr
from octop.infra.agentcomputer.identity import IdentityError, read_identity
from octop.infra.agents.resident_worker_lock import ResidentWorkerLock
from octop.infra.auth.box_accounts import BoxAccountService
from octop.infra.db.factory import open_database, resolve_sqlite_db_path
from octop.infra.db.migrate import run_migrations
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.utils.env_file import apply_env_file, env_file_path
from octop.infra.utils.paths import PathLayout


@contextmanager
def _maintenance(locale: str) -> Iterator[BoxAccountService]:
    paths = PathLayout.from_env()
    if not paths.config.is_file():
        raise click.ClickException(tr("cli.box.initialization_required", locale))
    apply_env_file(env_file_path(paths.root))
    config = load_config(paths.config)
    if config.account_mode != "box_local":
        raise click.ClickException(tr("cli.box.mode_required", locale))
    if not config.database.is_postgresql and not resolve_sqlite_db_path(config, paths).is_file():
        raise click.ClickException(tr("cli.box.initialization_required", locale))
    try:
        identity = read_identity(paths.agentcomputer_identity)
    except IdentityError as exc:
        raise click.ClickException(tr("cli.box.initialization_required", locale)) from exc
    lock = ResidentWorkerLock(paths.root / "box-auth" / "daemon.lock")
    try:
        lock.acquire()
    except RuntimeError as exc:
        raise click.ClickException(tr("cli.box.daemon_stop_required", locale)) from exc
    try:
        db = open_database(config, paths)
        try:
            run_migrations(db)
            services = build_shared_services(db=db, paths=paths, config=config)
            secret = services.secret_repo.get("jwt")
            if secret is None:
                raise click.ClickException(tr("cli.box.initialization_required", locale))
            service = BoxAccountService(services, secret=secret, box_id=str(identity.boxID))
            service.initialize()
            yield service
        except OctopError as exc:
            reason = str(exc.details.get("reason", ""))
            if reason == "legacy_upgrade_required":
                raise click.ClickException(tr("cli.box.legacy_upgrade_required", locale)) from exc
            raise click.ClickException(f"{exc.localized_message(locale)} ({reason})") from exc
        finally:
            db.close()
    finally:
        lock.release()


@click.group("box")
def box() -> None:
    """Manage a local box from its trusted host terminal."""


@box.group("key")
def key() -> None:
    """Initialize or rotate the management key while the daemon is stopped."""


@key.command("init")
@click.option(
    "--upgrade-legacy",
    is_flag=True,
    help="Explicitly revoke legacy clients and retain existing owner data.",
)
@click.option("--locale", type=click.Choice(["zh", "en"]), default="zh", show_default=True)
def key_init(upgrade_legacy: bool, locale: str) -> None:
    """Print the first management key once; only its hash is stored."""
    with _maintenance(locale) as service:
        result = service.initialize_management_key(upgrade_legacy=upgrade_legacy)
    click.echo(tr("cli.box.key_warning", locale), err=True)
    click.echo(result["management_key"])


@key.command("rotate")
@click.option(
    "--revoke-clients", is_flag=True, help="Also revoke all paired clients after credential theft."
)
@click.option("--locale", type=click.Choice(["zh", "en"]), default="zh", show_default=True)
def key_rotate(revoke_clients: bool, locale: str) -> None:
    """Replace the key and revoke web sessions and unused sharing codes."""
    with _maintenance(locale) as service:
        result = service.rotate_management_key_local(revoke_clients=revoke_clients)
    click.echo(tr("cli.box.key_warning", locale), err=True)
    click.echo(result["management_key"])


@box.command("pair", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option("--locale", type=click.Choice(["zh", "en"]), default="zh", show_default=True)
def pair(locale: str) -> None:
    """Retired: initialize a management key, then explicitly claim the primary client."""
    raise click.ClickException(tr("cli.box.pair_retired", locale))
