"""Signed, instance- and owner-scoped cursors for projects and conversations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError

from inhere_box.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from inhere_box.infra.db.services import SharedServices
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.projects.models import ProjectModel

CursorKind = Literal["sync", "history", "delta", "conversation", "task_events"]


class Cursor(ProjectModel):
    position: int = Field(default=0, ge=0)
    cutoff: int = Field(default=0, ge=0)
    after: str = Field(default="", max_length=256)
    updated_at: int = Field(default=0, ge=0)
    summary_revision: int = Field(default=0, ge=0)
    history_revision: int = Field(default=0, ge=0)
    history_position: int = Field(default=0, ge=0)


def cursor_reset() -> InhereBoxError:
    return InhereBoxError.localized(ErrorCode.PROJECT_SYNC_RESET)


@dataclass(frozen=True)
class CursorCodec:
    context: str
    key: bytes
    legacy_key: bytes | None = None

    def _signature(self, kind: str, raw: bytes, key: bytes | None = None) -> bytes:
        # Bind scope and purpose without carrying either in the URL.
        message = self.context.encode() + b"\0" + kind.encode() + b"\0" + raw
        return hmac.new(self.key if key is None else key, message, hashlib.sha256).digest()[
            : 8 if kind == "sync" else 16
        ]

    def encode(self, kind: CursorKind, **values: int | str) -> str:
        cursor = Cursor.model_validate(values)
        if kind == "sync":
            raw = cursor.position.to_bytes(max(1, (cursor.position.bit_length() + 7) // 8), "big")
        elif kind in {"history", "conversation"}:
            point = cursor.updated_at if kind == "history" else cursor.position
            raw = point.to_bytes(8, "big") + cursor.after.encode()
        elif kind == "task_events":
            raw = (
                b"".join(
                    point.to_bytes(8, "big")
                    for point in (
                        cursor.position,
                        cursor.summary_revision,
                        cursor.history_revision,
                        cursor.history_position,
                    )
                )
                + cursor.after.encode()
            )
        else:
            raw = (
                cursor.position.to_bytes(8, "big")
                + cursor.cutoff.to_bytes(8, "big")
                + cursor.after.encode()
            )
        return base64.urlsafe_b64encode(raw + self._signature(kind, raw)).decode().rstrip("=")

    def decode(self, token: str, kind: CursorKind) -> Cursor:
        try:
            if len(token) > 4096:
                raise ValueError("cursor too long")
            data = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(data).decode().rstrip("=") != token:
                raise ValueError("cursor encoding")
            signature_size = 8 if kind == "sync" else 16
            raw, signature = data[:-signature_size], data[-signature_size:]
            if not hmac.compare_digest(signature, self._signature(kind, raw)) and not (
                self.legacy_key is not None
                and hmac.compare_digest(signature, self._signature(kind, raw, self.legacy_key))
            ):
                raise ValueError("cursor signature")
            if kind == "sync":
                if not 1 <= len(raw) <= 8 or (len(raw) > 1 and raw[0] == 0):
                    raise ValueError("cursor size")
                return Cursor(position=int.from_bytes(raw, "big"))
            offset = 8 if kind in {"history", "conversation"} else 16
            if kind == "task_events":
                offset = 32
            if len(raw) < offset:
                raise ValueError("cursor size")
            after = raw[offset:].decode()
            if kind == "history":
                return Cursor(updated_at=int.from_bytes(raw[:8], "big"), after=after)
            if kind == "conversation":
                return Cursor(position=int.from_bytes(raw[:8], "big"), after=after)
            if kind == "task_events":
                return Cursor(
                    position=int.from_bytes(raw[:8], "big"),
                    summary_revision=int.from_bytes(raw[8:16], "big"),
                    history_revision=int.from_bytes(raw[16:24], "big"),
                    history_position=int.from_bytes(raw[24:32], "big"),
                    after=after,
                )
            return Cursor(
                position=int.from_bytes(raw[:8], "big"),
                cutoff=int.from_bytes(raw[8:16], "big"),
                after=after,
            )
        except (ValueError, TypeError, ValidationError, binascii.Error) as exc:
            raise cursor_reset() from exc


def cursor_codec(services: SharedServices, owner_id: int, scope: str) -> CursorCodec:
    try:
        identity = get_or_create_identity(services.paths.agentcomputer_identity)
    except (IdentityError, OSError) as exc:
        raise InhereBoxError.localized(ErrorCode.CONTROL_PLANE_BUSY) from exc
    secret = services.secret_repo.get("jwt")
    assert secret is not None
    return CursorCodec(
        context=f"v2:{identity.boxID}:{identity.restoreEpoch}:{owner_id}:{scope}",
        key=hmac.new(secret, b"inhere-box-list-sync-v2", hashlib.sha256).digest(),
        # Read pre-rename cursors; all newly issued cursors use the new domain.
        legacy_key=hmac.new(secret, b"octop-list-sync-v2", hashlib.sha256).digest(),
    )
