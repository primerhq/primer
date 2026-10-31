"""The turn persist of the workspace executor flushes the turn's event-log writer first (AG-M2).

A Message line carries no seq: the history reader tags it with the last seq before it. The persist used to write the
turn's lines while the writer still buffered the turn's records, so they were tagged with the turn's user_input seq
(see tests/session/test_message_log_order.py for the rewind that kept the answer).

Characterization (passes before and after): persist holds the messages lock across its commit (lock order
``messages_lock -> _commit_lock``), a persist with no writer bound writes as before, and a writer that has given up on
the workspace (``WorkspaceWriteTimeout``) does not stop the turn's messages.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from primer.model.chat import TextPart
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import WorkspaceMessageWriter, WorkspaceWriteTimeout
from tests._support.off_golden import FnLLM, assistant_message, make_executor, open_session, text_events


def _text(message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


def _lines(workspace, session) -> list[dict]:
    path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# AG-M2: the persist flushes the bound writer first
# ---------------------------------------------------------------------------


class _WorkspaceIO:
    """The dispatch writer's production backend: an O_APPEND line under the session's messages lock."""

    def __init__(self, workspace):
        self.workspace = workspace

    async def append_message_line(self, session_id, line):
        await self.workspace.append_message_line(session_id, line)


def _record(kind: SessionMessageKind) -> SessionMessageRecord:
    return SessionMessageRecord(seq=1, kind=kind, payload={}, created_at=datetime.now(timezone.utc))


async def test_the_persist_writes_the_turns_lines_after_the_records_the_writer_buffered(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        writer = WorkspaceMessageWriter(workspace_io=_WorkspaceIO(workspace), session_id=session.session_id, start_seq=10)
        await writer.append(_record(SessionMessageKind.REASONING))
        await writer.append(_record(SessionMessageKind.ASSISTANT_TOKEN))
        ex = make_executor(session, FnLLM(lambda *a: text_events("x")), configure=lambda e: e.bind_event_log(writer))
        await asyncio.wait_for(ex._persist_turn([assistant_message("ANSWER")]), timeout=10)
        tail = [(o.get("seq"), o.get("kind") or o.get("role")) for o in _lines(workspace, session)][-3:]
        assert tail == [(11, "reasoning"), (12, "assistant_token"), (None, "assistant")]
    finally:
        await session.aclose()
        await backend.aclose()


async def test_a_persist_with_no_writer_bound_writes_the_turn(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        ex = make_executor(session, FnLLM(lambda *a: text_events("x")))
        await ex._persist_turn([assistant_message("ANSWER")])
        assert [_text(m) for m in await ex._read_messages_jsonl()][-1] == "ANSWER"
    finally:
        await session.aclose()
        await backend.aclose()


async def test_a_writer_that_gave_up_on_the_workspace_does_not_stop_the_turns_messages(tmp_path):
    class _Broken:
        async def reserve_seq(self) -> int:
            raise WorkspaceWriteTimeout("the writer is closed")

        async def flush(self) -> None:
            raise WorkspaceWriteTimeout("the writer is closed")

    backend, workspace, session = await open_session(tmp_path)
    try:
        ex = make_executor(session, FnLLM(lambda *a: text_events("x")), configure=lambda e: e.bind_event_log(_Broken()))
        await ex._persist_turn([assistant_message("ANSWER")])
        assert [_text(m) for m in await ex._read_messages_jsonl()][-1] == "ANSWER"
    finally:
        await session.aclose()
        await backend.aclose()


async def test_the_persist_holds_the_messages_lock_across_its_commit(tmp_path):
    """Lock order ``messages_lock -> _commit_lock``: commit_state (which takes the latter) runs under the former."""
    backend, workspace, session = await open_session(tmp_path)
    try:
        held: list[bool] = []
        real_commit = session.commit_state

        async def commit_state(**kwargs):
            try:
                async with asyncio.timeout(0.05):
                    async with session.messages_lock:
                        held.append(False)
            except TimeoutError:
                held.append(True)
            return await real_commit(**kwargs)

        session.commit_state = commit_state
        ex = make_executor(session, FnLLM(lambda *a: text_events("x")))
        await ex._persist_turn([assistant_message("ANSWER")])
        assert held == [True]
    finally:
        await session.aclose()
        await backend.aclose()
