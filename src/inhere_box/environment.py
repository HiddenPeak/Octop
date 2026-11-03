"""Environment compatibility for installations created before the rename."""

from __future__ import annotations

import os
from typing import overload


@overload
def env_get(key: str, default: None = None) -> str | None: ...


@overload
def env_get(key: str, default: str) -> str: ...


def env_get(key: str, default: str | None = None) -> str | None:
    """Prefer INHERE_BOX_*; accept the corresponding legacy OCTOP_* key."""
    if key in os.environ:
        return os.environ[key]
    if key.startswith("INHERE_BOX_"):
        return os.environ.get("OCTOP_" + key.removeprefix("INHERE_BOX_"), default)
    return os.environ.get(key, default)
