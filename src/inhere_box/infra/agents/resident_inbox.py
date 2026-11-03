"""Private conversation read state shared by all authenticated devices."""

from __future__ import annotations

from pydantic import Field

from inhere_box.infra.agents.resident_history import require_resident_binding
from inhere_box.infra.db.services import SharedServices
from inhere_box.infra.errors import ErrorCode, InhereBoxError
from inhere_box.infra.projects.models import ProjectModel


class ResidentInbox(ProjectModel):
    agent_id: str
    thread_id: str
    unread_count: int
    last_message_id: str | None
    last_read_message_id: str | None
    revision: int


class ResidentRead(ProjectModel):
    message_id: str = Field(
        min_length=1,
        max_length=250,
        description="The last visible assistant receipt, task report or proactive message actually read. The watermark only moves forward.",
    )


def inbox_state(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    message_id: str | None = None,
) -> ResidentInbox:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    repo = services.resident_inbox_repo
    state = (
        repo.mark_read(owner_id, thread_id, message_id)
        if message_id is not None
        else repo.state(owner_id, thread_id)
    )
    if state is None:
        raise InhereBoxError.localized(ErrorCode.RESIDENT_READ_ANCHOR_INVALID)
    return ResidentInbox(agent_id=agent_id, thread_id=thread_id, **state)
