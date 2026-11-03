"""tests/unit/test_shared.py"""

from __future__ import annotations

from pathlib import Path

from inhere_box.config import InhereBoxConfig
from inhere_box.infra.db.migrate import run_migrations
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.repos.sso import SsoRepo
from inhere_box.infra.db.services import SharedServices, build_shared_services
from inhere_box.infra.utils.paths import PathLayout


def test_build_shared_services(tmp_path: Path):
    cfg = InhereBoxConfig()
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=cfg)
    assert isinstance(services, SharedServices)
    assert services.db is db
    assert services.paths is paths
    assert services.config is cfg
    # all repos resolved
    assert services.user_repo.count() == 0
    assert isinstance(services.sso_repo, SsoRepo)
