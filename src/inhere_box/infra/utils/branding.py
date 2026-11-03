"""Read renamed application metadata without rewriting historical records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def legacy_metadata_key(key: str) -> str:
    return key.replace("inhere_box_", "octop_", 1)


def metadata_value(data: Mapping[str, Any], key: str, default: Any = None) -> Any:
    if key in data:
        return data[key]
    return data.get(legacy_metadata_key(key), default)


def pop_metadata(data: dict[str, Any], key: str, default: Any = None) -> Any:
    legacy = data.pop(legacy_metadata_key(key), default)
    return data.pop(key, legacy)
