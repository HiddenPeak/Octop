"""``errors.*`` — API / InhereBoxError messages."""

from __future__ import annotations

from inhere_box.i18n.loader import tr
from inhere_box.infra.utils.locale import Locale

__all__ = ["error_message"]


def error_message(code: str, locale: str | Locale = "en", **kwargs: object) -> str:
    return tr(f"errors.{code}", locale, **kwargs)
