"""Durable, database-external authentication fence.

A journal left by an interrupted mutation always fails closed. Recovery is a
separate local maintenance operation, not an automatic choice of an old DB.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from inhere_box.infra.auth.box_accounts.crypto import canonical


class TrustStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.journal = path.with_name("trust-intent.json")

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name != "posix":
            return
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, path: Path, value: object, *, create_only: bool = False) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=".auth-", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(canonical(value) + "\n")
                file.flush()
                os.fsync(file.fileno())
            if create_only:
                os.link(temporary, path)
            else:
                os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self) -> dict[str, Any]:
        if self.journal.exists():
            raise ValueError("unfinished trust change")
        record = json.loads(self.path.read_text("utf-8"))
        if not isinstance(record, dict):
            raise ValueError("invalid trust record")
        return record

    def initialize(self, record: dict[str, Any]) -> None:
        with suppress(FileExistsError):
            self._write(self.path, record, create_only=True)

    def stage(self, previous: dict[str, Any], target: dict[str, Any]) -> None:
        self._write(self.journal, {"previous": previous, "target": target}, create_only=True)

    def publish(self, target: dict[str, Any]) -> None:
        self._write(self.path, target)
        self.journal.unlink()
        self._sync_directory(self.path.parent)
