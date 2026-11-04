"""Open a database pool from process configuration."""

from __future__ import annotations

from pathlib import Path

from inhere_box.config import InhereBoxConfig, database_env_configured
from inhere_box.infra.db.pool import DatabasePool, PostgresPool, SqlitePool
from inhere_box.infra.utils.paths import PathLayout


def resolve_sqlite_db_path(config: InhereBoxConfig, paths: PathLayout) -> Path:
    """Return the SQLite file path that ``open_database`` would use for sqlite."""
    from inhere_box.infra.setup.branding_migration import require_installation_migrated

    db_cfg = config.database
    database = (
        db_cfg.resolve_sqlite_path(paths.root)
        if config.database_in_file or database_env_configured()
        else paths.db
    )
    require_installation_migrated(paths.root, database)
    return database


def should_defer_control_plane_db(config: InhereBoxConfig, paths: PathLayout) -> bool:
    """True when first-run setup should delay opening the control-plane DB.

    Defer only for greenfield SQLite installs with no existing DB file and no
    ``INHERE_BOX_DATABASE_*`` / PostgreSQL configuration. Password verification and
    early wizard steps then run without a pool.
    """
    from inhere_box.infra.setup.branding_migration import require_installation_migrated

    require_installation_migrated(paths.root)
    if config.account_mode == "box_local" or config.database.is_postgresql:
        return False
    if database_env_configured():
        return False
    return not resolve_sqlite_db_path(config, paths).exists()


def open_database(config: InhereBoxConfig, paths: PathLayout) -> DatabasePool:
    """Return a DB pool for the configured driver.

    When ``config.json`` has no ``database`` section and no ``INHERE_BOX_DATABASE_*``
    env overrides are set, uses the installation's canonical database file.
    """
    from inhere_box.infra.setup.branding_migration import require_installation_migrated

    require_installation_migrated(paths.root)
    db_cfg = config.database
    if db_cfg.is_postgresql:
        return PostgresPool(db_cfg.postgresql_conninfo())

    return SqlitePool(resolve_sqlite_db_path(config, paths))
