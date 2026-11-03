"""Keep daemon and embedded conversation writers out of an offline restore."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from inhere_box.infra.agentcomputer.bootstrap import DaemonBootstrap
from inhere_box.infra.agents.resident_worker_lock import ResidentWorkerLock
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.utils.paths import PathLayout


@contextmanager
def offline_restore_guard(paths: PathLayout) -> Iterator[None]:
    """Hold both lifetime locks until registry and filesystem restoration finishes."""
    controller = DaemonBootstrap(paths.agentcomputer_identity.parent)
    resident = ResidentWorkerLock(paths.root / "resident-worker.lock")
    with ExitStack() as cleanup:
        try:
            controller.acquire_sync()
        except InhereBoxError as exc:
            if exc.code == ErrorCode.CONTROL_PLANE_BUSY:
                raise InhereBoxError.localized(ErrorCode.BACKUP_OFFLINE_REQUIRED) from exc
            raise
        cleanup.callback(controller.close_sync)
        try:
            resident.acquire()
        except RuntimeError as exc:
            raise InhereBoxError.localized(ErrorCode.BACKUP_OFFLINE_REQUIRED) from exc
        cleanup.callback(resident.release)
        yield
