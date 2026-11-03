"""Golden transcripts of the session message log (Task 14, H2, Stage 0).

``messages.jsonl`` is the system of record of a session's transcript; Stage 1 changes how it is READ, Stage 2 how it is WRITTEN, behind a flag. Neither
may change what ends up on disk or what a reader makes of it. So each scenario here drives the REAL session and executor over a real local workspace,
then records, normalised (timestamps and ids are the only bytes that differ between runs):

* the bytes of ``messages.jsonl`` (and a digest of them),
* the shape of the ``.state/`` git history: how many commits, and each one's subject and the files it touched,
* the history a reader reconstructs (marker folded), as role/part fingerprints,
* ``_max_event_log_seq`` of the file,
* what ``take_pending_messages`` returns.

``tests/session/test_message_log_golden.py`` compares a fresh run with ``tests/session/fixtures/message_log_golden/<scenario>.json``; that file was captured from
the code BEFORE Stage 1 and is the contract for every later stage, with the append-only flag both off and on. Regenerate it only deliberately:
``PRIMER_CAPTURE_LOG_GOLDEN=1 uv run pytest tests/session/test_message_log_golden.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from primer.agent.workspace_executor import _max_event_log_seq
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord, SessionStatus, _UserInputWaiting
from primer.session.persistence import WorkspaceMessageWriter
from tests._support.off_golden import (
    Events, FnLLM, ScriptedLLM, assistant_message, make_executor, normalise, open_session, text_events, user_message,
)


def tool_turn(i: int) -> list[Message]:
    """A small tool round: the model calls a tool, the result comes back, the model answers."""
    return [
        Message(role="assistant", parts=[ToolCallPart(id=f"call_{i}", name="exec", arguments={"cmd": f"echo {i}"})]),
        Message(role="tool", parts=[ToolResultPart(id=f"call_{i}", output=f"out {i}\n")]),
        assistant_message(f"answered {i}"),
    ]


def record(seq: int, kind: SessionMessageKind, payload: dict[str, Any] | None = None) -> SessionMessageRecord:
    when = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seq)
    return SessionMessageRecord(seq=seq, kind=kind, payload=payload or {}, created_at=when)


class Env:
    """What a scenario drives: the workspace, the session, an executor over it, and a writer for the dispatch layer's event rows."""

    def __init__(self, workspace, session, llm, append_only: bool):
        self.workspace, self.session, self.llm, self.append_only = workspace, session, llm, append_only
        session.session_log_append_only = append_only
        self.ex = self.executor()

    def executor(self):
        return make_executor(self.session, self.llm)

    @property
    def messages_path(self) -> Path:
        return self.workspace.root / self.workspace.template.state_path / "sessions" / self.session.session_id / "messages.jsonl"

    def writer(self, start_seq: int = 0) -> WorkspaceMessageWriter:
        workspace, session_id = self.workspace, self.session.session_id

        class _IO:
            async def append_message_line(self, sid, line):
                await workspace.append_message_line(sid, line)

        return WorkspaceMessageWriter(workspace_io=_IO(), session_id=session_id, start_seq=start_seq)

    async def event_line(self, rec: SessionMessageRecord) -> None:
        await self.workspace.append_message_line(self.session.session_id, (rec.model_dump_json() + "\n").encode())


# ---------------------------------------------------------------------------------------------- scenarios

Scenario = Callable[[Env], Awaitable[None]]


async def plain_turns(env: Env) -> None:
    llm = env.llm
    llm.extend([Events(text_events("first answer")), Events(text_events("second answer"))])
    async for _ in env.ex.invoke([]):
        pass
    await env.session.append_instruction("a follow-up")
    async for _ in env.executor().invoke([]):
        pass


async def tool_calls(env: Env) -> None:
    await env.ex._persist_turn([user_message("run two things")])
    await env.ex._persist_turn(tool_turn(1))
    await env.ex._persist_turn(tool_turn(2))


async def compaction_marker(env: Env) -> None:
    await env.ex._persist_turn([user_message("one"), assistant_message("two"), user_message("three"), assistant_message("four")])
    summary = Message(role="assistant", parts=[TextPart(text="[earlier conversation compacted]\n\nSUMMARY: one/two")])
    kept = [user_message("three"), assistant_message("four")]
    await env.ex._replace_compacted_head(
        [summary, *kept], summary_message=summary, tokens_before=900, tokens_after=100, trigger_tokens=800,
    )
    await env.ex._persist_turn([user_message("after the marker"), assistant_message("noted")])


async def steer_during_turn(env: Env) -> None:
    await env.ex._persist_turn([user_message("start")])
    persist = asyncio.ensure_future(env.ex._persist_turn([assistant_message("working"), assistant_message("done")]))
    steer = asyncio.ensure_future(env.session.append_instruction("steer: also check Y"))
    await asyncio.gather(persist, steer)
    await env.ex._persist_turn([assistant_message("saw the steer")])


async def deferred_steers(env: Env) -> None:
    await env.ex._persist_turn([user_message("long task"), assistant_message("on it")])
    await env.ex._open_compaction_window()
    await env.session.append_instruction("deferred one")
    await env.session.append_instruction("deferred two")
    summary = Message(role="assistant", parts=[TextPart(text="[earlier conversation compacted]\n\nSUMMARY: long task")])
    await env.ex._replace_compacted_head([summary], summary_message=summary, tokens_before=500, tokens_after=50, trigger_tokens=400)
    await env.ex._close_compaction_window()
    await env.ex._persist_turn([assistant_message("continued")])


async def pause_resume(env: Env) -> None:
    await env.ex._persist_turn([user_message("ask me"), assistant_message("which one?")])
    await env.session.set_status(SessionStatus.WAITING, waiting_state=_UserInputWaiting(prompt="which one?", queued_at=datetime(2026, 11, 1, tzinfo=timezone.utc)))
    await env.session.append_instruction("the second one")
    await env.session.set_status(SessionStatus.RUNNING)
    await env.ex.inject_resume_messages([
        Message(role="assistant", parts=[ToolCallPart(id="park_1", name="ask_user", arguments={"q": "which one?"})]),
        Message(role="tool", parts=[ToolResultPart(id="park_1", output="the second one")]),
    ])
    await env.ex._persist_turn([assistant_message("second it is")])


async def graph_session(env: Env) -> None:
    """The dispatch layer's event rows (what a graph node's session streams) interleaved with the executor's persists."""
    writer = env.writer(start_seq=0)
    await writer.append(record(1, SessionMessageKind.USER_INPUT, {"text": "node input"}))
    env.ex.bind_event_log(writer)
    await writer.append(record(2, SessionMessageKind.REASONING, {"text": "thinking"}))
    await writer.append(record(3, SessionMessageKind.ASSISTANT_TOKEN, {"text": "tok"}))
    await env.ex._persist_turn([user_message("node input"), assistant_message("node output")])
    await writer.append(record(4, SessionMessageKind.DONE, {}))
    await writer.flush()


async def canvas_records(env: Env) -> None:
    await env.ex._persist_turn([user_message("draw a box")])
    await env.event_line(record(1, SessionMessageKind.CANVAS_OP, {"op": "add", "id": "box"}))
    await env.event_line(record(2, SessionMessageKind.CANVAS_EVENT, {"event": "click", "id": "box"}))
    await env.ex._persist_turn([assistant_message("drawn")])
    await env.event_line(record(3, SessionMessageKind.CANVAS_EVENT, {"event": "click", "id": "box"}))


SCENARIOS: dict[str, Scenario] = {
    "plain_turns": plain_turns,
    "tool_calls": tool_calls,
    "compaction_marker": compaction_marker,
    "steer_during_turn": steer_during_turn,
    "deferred_steers": deferred_steers,
    "pause_resume": pause_resume,
    "graph_session": graph_session,
    "canvas_records": canvas_records,
}


# ---------------------------------------------------------------------------------------------- capture


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _fingerprint(message: Message, session_id: str) -> dict[str, Any]:
    return {
        "role": message.role,
        "parts": [
            [getattr(p, "type", type(p).__name__), _digest(normalise(json.dumps(p.model_dump(mode="json"), sort_keys=True), session_id))]
            for p in message.parts
        ],
        "text": normalise("".join(p.text for p in message.parts if isinstance(p, TextPart)), session_id)[:80],
    }


def git_shape(state_path: Path, session_id: str) -> list[dict[str, Any]]:
    """Each commit of ``.state/`` oldest first: its subject (normalised) and the files it touched."""
    out = subprocess.run(
        ["git", "-C", str(state_path), "log", "--reverse", "--name-only", "--format=@@%s"],
        check=True, capture_output=True, text=True,
    ).stdout
    commits: list[dict[str, Any]] = []
    for block in out.split("@@")[1:]:
        subject, *files = [line for line in block.splitlines() if line.strip()] or [""]
        commits.append({
            "subject": normalise(subject, session_id).replace(session_id[-12:], "<sid12>"),
            "files": sorted(normalise(f, session_id) for f in files),
        })
    return commits


def segment_paths(slot: Path) -> list[Path]:
    """The files of a session's log in order: ``messages.jsonl``, ``messages.0001.jsonl`` ..."""
    from primer.workspace.segments import ordered

    return [slot / name for _, name in ordered(p.name for p in slot.glob("messages*.jsonl"))]


async def capture(name: str, tmp_path: Path, *, append_only: bool = False, segment_bytes: int = 0) -> dict[str, Any]:
    """The golden record of a scenario. With ``segment_bytes`` the log rotates at that size, and the record is of the log as a reader sees it (the
    segments joined), plus ``"segments"``: their sizes (a key the single-file golden does not have)."""
    from primer.workspace.segments import set_segment_bytes_default

    llm = ScriptedLLM()
    set_segment_bytes_default(segment_bytes)
    backend, workspace, session = await open_session(tmp_path)
    try:
        llm.session_id = session.session_id
        env = Env(workspace, session, llm, append_only)
        await SCENARIOS[name](env)
        slot = env.messages_path.parent
        parts = segment_paths(slot)
        text = b"".join(p.read_bytes() for p in parts).decode("utf-8")
        normalised = normalise(text, session.session_id)
        history = await env.executor()._read_messages_jsonl()
        pending = await session.take_pending_messages()
        extra = {"segments": [p.stat().st_size for p in parts]} if segment_bytes else {}
        return {
            **extra,
            "messages_jsonl": {"bytes": len(text.encode()), "sha": _digest(normalised), "lines": normalised.splitlines()},
            "git": git_shape(workspace.root / workspace.template.state_path, session.session_id),
            "history": [_fingerprint(m, session.session_id) for m in history],
            "max_event_log_seq": _max_event_log_seq(text),
            "pending": [_fingerprint(m, session.session_id) for m in pending],
        }
    finally:
        set_segment_bytes_default(0)
        await session.aclose()
        await backend.aclose()
