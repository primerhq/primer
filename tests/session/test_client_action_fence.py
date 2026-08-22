"""The replay fence: attach mark sits strictly below every fresh record.

S3 spec section 4 (crosscheck M3): the browser executes only records with
seq ABOVE the current attachment's attach-time high-water mark. That rule
is only safe if the writer that appends a delivery record always assigns a
seq above the session's last_seq at attach time. This pins that.
"""

from __future__ import annotations

from datetime import UTC, datetime

from primer.model.client_attachment import ClientAttachment
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.attachment import attach_or_refresh
from primer.session.persistence import WorkspaceMessageWriter
from tests.conftest import _InMemoryStorage


class _FakeIO:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        del session_id
        self.lines.append(line)


def _record() -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.CLIENT_ACTION,
        payload={"call_id": "tc-1", "name": "client__open_file", "arguments": {}},
        created_at=datetime.now(UTC),
    )


async def test_fresh_delivery_lands_above_the_attach_mark() -> None:
    storage = _InMemoryStorage(ClientAttachment)
    session_last_seq = 12
    att = await attach_or_refresh(
        storage,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=session_last_seq,
    )
    writer = WorkspaceMessageWriter(
        workspace_io=_FakeIO(), session_id="s-1", start_seq=session_last_seq,
    )
    seq = await writer.append(_record())
    assert seq > att.attached_seq


async def test_records_written_before_the_attach_sit_at_or_below_it() -> None:
    storage = _InMemoryStorage(ClientAttachment)
    writer = WorkspaceMessageWriter(
        workspace_io=_FakeIO(), session_id="s-1", start_seq=0,
    )
    earlier = await writer.append(_record())
    att = await attach_or_refresh(
        storage,
        workspace_id="ws-1",
        session_id="s-1",
        client_id="tab-a",
        last_seq=writer.last_seq,
    )
    assert earlier <= att.attached_seq
