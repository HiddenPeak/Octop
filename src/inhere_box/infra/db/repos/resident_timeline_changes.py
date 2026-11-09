"""Index the latest committed change to each visible resident timeline item."""

from __future__ import annotations

from typing import Any


def record_timeline_change(conn: Any, thread_id: str, item_id: str) -> None:
    # The caller has already incremented the thread revision in this transaction.
    conn.execute(
        "INSERT INTO resident_timeline_changes(thread_id,item_id,revision) "
        "SELECT thread_id,?,revision FROM resident_conversations WHERE thread_id=? "
        "ON CONFLICT(thread_id,item_id) DO UPDATE SET revision=excluded.revision",
        (item_id, thread_id),
    )
