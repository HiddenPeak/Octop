"""Machine-readable knowledge citation payload embedded in tool results."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, TypedDict

CITATIONS_MARKER_PREFIX = "<!--inhere-box-kb-citations:"
CITATIONS_MARKER_SUFFIX = "-->"


class KnowledgeCitation(TypedDict):
    kb_id: str
    kb_name: str
    doc_id: str
    filename: str
    path: str


def citations_from_ranked(ranked: Sequence[tuple[Any, Any, Any]]) -> list[KnowledgeCitation]:
    """Deduplicate ranked hits to one citation per document (retrieval order)."""
    seen: set[str] = set()
    out: list[KnowledgeCitation] = []
    for base, _hit, document in ranked:
        doc_id = str(getattr(document, "id", "") or "").strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        out.append(
            {
                "kb_id": str(getattr(base, "id", "") or ""),
                "kb_name": str(getattr(base, "name", "") or ""),
                "doc_id": doc_id,
                "filename": str(getattr(document, "filename", "") or doc_id),
                "path": str(getattr(document, "path", "") or ""),
            }
        )
    return out


_CITATIONS_PREFIX_RE = re.compile(r"<!--(?:inhere-box|octop)-kb-citations:")
_CITATIONS_MARKER_RE = re.compile(
    r"<!--(?:inhere-box|octop)-kb-citations:(\[.*\])-->\s*", re.DOTALL
)


def append_citations_marker(text: str, citations: Sequence[KnowledgeCitation]) -> str:
    """Append a HTML-comment marker the dashboard can parse; LLM usually ignores it."""
    body = (text or "").rstrip()
    if not body or not citations:
        return body
    payload = json.dumps(list(citations), ensure_ascii=False, separators=(",", ":"))
    return f"{body}\n\n{CITATIONS_MARKER_PREFIX}{payload}{CITATIONS_MARKER_SUFFIX}"


def strip_citations_marker(text: str) -> str:
    """Read both marker names without changing persisted messages or invalid prose."""
    while text:
        for prefix in reversed(list(_CITATIONS_PREFIX_RE.finditer(text))):
            marker = _CITATIONS_MARKER_RE.fullmatch(text, prefix.start())
            if marker is None:
                continue
            try:
                payload = json.loads(marker[1])
            except ValueError:
                continue
            if not isinstance(payload, list):
                continue
            text = text[: marker.start()].rstrip()
            break
        else:
            break
    return text
