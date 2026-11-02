"""The reader matrix of the segmented session log (seglog Task 1, characterization).

``messages.jsonl`` is read by many places (design section 8). Segmentation (``messages.jsonl`` = segment 0, then ``messages.0001.jsonl`` ...) must not change what
ANY of them answers. This module builds logs (the normalised bytes of the message-log golden scenarios, plus one hand-built run with every record kind), cuts them
into segments at record boundaries, installs them in a real local workspace, and runs EVERY reader on the result, returning a comparable value per reader.

``tests/session/test_segmented_readers_golden.py`` pins each reader's answer on the single-file log (the contract), and requires the segmented form to give the same
answer for every reader that is not listed in ``KNOWN_SEGMENT_GAPS``; each task that makes a reader segment-aware removes it from that list.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
from primer.model.workspace_session import AgentSessionBinding, SessionMessageKind, SessionMessageRecord, SessionStatus, WorkspaceSession
from tests._support.log_golden import SCENARIOS, _fingerprint
from tests._support.off_golden import ScriptedLLM, make_executor, open_session

FIXTURES = Path(__file__).parents[1] / "session" / "fixtures" / "message_log_golden"
WS = "ws-seg"
FIXED_TS = "2026-11-01T12:00:00+00:00"
_WHEN = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

#: Readers that do not yet read a segmented log (they open ``messages.jsonl`` alone). Each seglog task that fixes a reader deletes its name here; a name left
#: in the list that no longer fails anywhere is itself a test failure, so the list can only shrink.
KNOWN_SEGMENT_GAPS: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------------------------- the logs


def _rec(seq: int, kind: SessionMessageKind, payload: dict[str, Any] | None = None) -> str:
    return SessionMessageRecord(seq=seq, kind=kind, payload=payload or {}, created_at=_WHEN).model_dump_json()


def _msg(role: str, *parts: Any) -> str:
    return Message(role=role, parts=list(parts)).model_dump_json()  # type: ignore[arg-type]


def full_run() -> bytes:
    """Six turns with every record kind the readers care about: tools, reasoning, streamed text, a compaction and its kept tail, a rewind, an error."""
    K = SessionMessageKind
    kept = [Message(role="user", parts=[TextPart(text="second question")]).model_dump(mode="json"),
            Message(role="assistant", parts=[TextPart(text="second answer")]).model_dump(mode="json")]
    seq = [0]

    def rec(kind: SessionMessageKind, payload: dict[str, Any] | None = None) -> str:
        seq[0] += 1
        return _rec(seq[0], kind, payload)

    def turn(question: str, answer: str) -> list[str]:
        return [rec(K.USER_INPUT, {"text": question}), _msg("user", TextPart(text=question)), _msg("assistant", TextPart(text=answer)),
                rec(K.ASSISTANT_TOKEN, {"text": answer}), rec(K.DONE, {"stop_reason": "stop"})]

    lines = [
        rec(K.USER_INPUT, {"text": "first question"}), _msg("user", TextPart(text="first question")),
        rec(K.REASONING, {"text": "thinking"}),
        rec(K.TOOL_CALL, {"id": "c1", "name": "exec", "arguments": {"cmd": "ls"}}),
        _msg("assistant", ToolCallPart(id="c1", name="exec", arguments={"cmd": "ls"})),
        rec(K.TOOL_RESULT, {"id": "c1", "output": "a b"}), _msg("tool", ToolResultPart(id="c1", output="a b")),
        _msg("assistant", TextPart(text="first answer")), rec(K.ASSISTANT_TOKEN, {"text": "first answer"}), rec(K.DONE, {"stop_reason": "stop"}),
        *turn("second question", "second answer"),
        rec(K.COMPACTION_MARKER, {"summary": "SUMMARY of the first turn", "kept_tail_messages": kept, "tokens_before": 900, "tokens_after": 120}),
    ]
    third = seq[0] + 1
    lines += turn("third question", "third answer")
    lines += [rec(K.USER_INPUT, {"text": "fourth question"}), _msg("user", TextPart(text="fourth question")), rec(K.REWIND_MARKER, {"to_seq": third})]
    lines += [rec(K.USER_INPUT, {"text": "fifth question"}), _msg("user", TextPart(text="fifth question")), rec(K.ERROR, {"message": "boom"})]
    lines += turn("sixth question", "sixth answer")
    return ("\n".join(lines) + "\n").encode()


def open_tail() -> bytes:
    """:func:`full_run` without its last answer and ``done``: the sixth question is still waiting (a pending message, an open turn)."""
    lines = full_run().decode().splitlines()
    return ("\n".join(lines[:-3]) + "\n").encode()


def scenario_logs() -> dict[str, bytes]:
    """name -> log bytes: the normalised golden scenarios (timestamps and the session id replaced by fixed values) and :func:`full_run`."""
    out: dict[str, bytes] = {}
    for path in sorted(FIXTURES.glob("*.json")):
        lines = json.loads(path.read_text())["messages_jsonl"]["lines"]
        text = "\n".join(lines) + "\n"
        out[path.stem] = text.replace("<ts>", FIXED_TS).replace("<sid>", "sess-fixed").encode()
    assert set(out) == set(SCENARIOS), "a golden scenario has no fixture (regenerate the message-log golden first)"
    out["full_run"] = full_run()
    out["open_tail"] = open_tail()
    return out


# ---------------------------------------------------------------------------------------------- cutting


def boundaries(raw: bytes) -> list[int]:
    """Every offset right after a ``\\n`` (a record boundary), the end excluded."""
    return [i + 1 for i, b in enumerate(raw) if b == 0x0A and i + 1 < len(raw)]


def split_log(raw: bytes, points: list[int]) -> list[bytes]:
    cuts = sorted(set(points))
    ok = set(boundaries(raw))
    if any(c not in ok for c in cuts):
        raise ValueError("a cut is not a record boundary")
    edges = [0, *cuts, len(raw)]
    return [raw[a:b] for a, b in zip(edges, edges[1:])]


def _after(raw: bytes, needle: bytes) -> list[int]:
    out, at = [], 0
    for line in raw.splitlines(keepends=True):
        at += len(line)
        if needle in line and at < len(raw):
            out.append(at)
    return out


def _before(raw: bytes, needle: bytes) -> list[int]:
    out, at = [], 0
    for line in raw.splitlines(keepends=True):
        if needle in line and at > 0:
            out.append(at)
        at += len(line)
    return out


#: name -> the cut points for a log. A plan whose cuts are empty leaves one segment.
PLANS: dict[str, Callable[[bytes], list[int]]] = {
    "one": lambda raw: [],
    "half": lambda raw: [min(boundaries(raw), key=lambda b: abs(b - len(raw) // 2))] if boundaries(raw) else [],
    "every_record": boundaries,
    "around_marker": lambda raw: [*_before(raw, b'"compaction_marker"'), *_after(raw, b'"compaction_marker"')],
    "after_every_terminal": lambda raw: _after(raw, b'"kind":"done"') + _after(raw, b'"kind":"error"'),
    "tiny_tail": lambda raw: [boundaries(raw)[-1]] if boundaries(raw) else [],
}


def install(env: "Env", parts: list[bytes]) -> None:
    """Write the log into the session's slot: ``messages.jsonl``, ``messages.0001.jsonl`` ... Anything already there is removed."""
    slot = env.slot
    for old in slot.glob("messages*.jsonl"):
        old.unlink()
    text_parts = [p.replace(b"sess-fixed", env.sid.encode()) for p in parts]
    for n, part in enumerate(text_parts):
        (slot / ("messages.jsonl" if n == 0 else f"messages.{n:04d}.jsonl")).write_bytes(part)


# ---------------------------------------------------------------------------------------------- the environment


class _Registry:
    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace if workspace_id == WS else None


class Env:
    """A real local workspace and session, a session row for the readers that take one, a registry, a shim, and an in-memory session store."""

    def __init__(self, backend: Any, workspace: Any, session: Any) -> None:
        from primer.worker.io_shim import _WorkspaceIOShim
        from tests.conftest import _InMemoryStorage

        self.backend, self.workspace, self.session = backend, workspace, session
        self.sid = session.session_id
        self.state_path = workspace.template.state_path
        self.slot = workspace.root / self.state_path / "sessions" / self.sid
        self.registry = _Registry(workspace)
        self.shim = _WorkspaceIOShim(workspace_registry=self.registry)
        self.shim.register_session(self.sid, WS)
        self.row = WorkspaceSession(
            id=self.sid, workspace_id=WS, binding=AgentSessionBinding(agent_id="ag-seg"), created_at=_WHEN, status=SessionStatus.RUNNING, turn_status="idle",
        )
        self.store = _InMemoryStorage(WorkspaceSession)
        self.slot_rel = f"{self.state_path}/sessions/{self.sid}"

    @property
    def provider(self) -> Any:
        store = self.store

        class _Provider:
            def get_storage(self, _cls: Any) -> Any:
                return store

        return _Provider()

    async def close(self) -> None:
        await self.session.aclose()
        await self.backend.aclose()


async def open_env(tmp_path: Path) -> Env:
    backend, workspace, session = await open_session(tmp_path)
    env = Env(backend, workspace, session)
    await env.store.create(env.row)
    return env


# ---------------------------------------------------------------------------------------------- the readers


def _norm(value: Any, sid: str) -> Any:
    """The value as JSON-comparable data, the session id replaced."""
    return json.loads(json.dumps(value, sort_keys=True, default=str).replace(sid, "<sid>"))


def _msgs(messages: list[Message], sid: str) -> list[Any]:
    return [_fingerprint(m, sid) for m in messages]


async def r_history(env: Env) -> Any:
    return _msgs((await env.session.log_view()).history, env.sid)


async def r_compaction_state(env: Env) -> Any:
    return list((await env.session.log_view()).compaction)


async def r_max_event_seq(env: Env) -> Any:
    return (await env.session.log_view()).max_seq


async def r_take_pending(env: Env) -> Any:
    return _msgs(await env.session.take_pending_messages(), env.sid)


async def r_executor_history(env: Env) -> Any:
    return _msgs(await make_executor(env.session, ScriptedLLM())._read_messages_jsonl(), env.sid)


async def r_usage_totals(env: Env) -> Any:
    from primer.api.routers.sessions import _session_usage_totals
    from primer.api.routers.tap import usage_frame
    from primer.session.usage_cache import UsageCache, fold_usage
    from primer.workspace.segments import session_log_for

    folded = usage_frame(await fold_usage(session_log_for(env.workspace, env.sid), UsageCache(), ("seg", env.sid)))
    return {"fold": folded, "totals": await _session_usage_totals(env.row, env.registry)}


async def r_transcript_page(env: Env) -> Any:
    from primer.api.routers.sessions import _read_workspace_turn_log
    from primer.workspace.segments import session_log_for

    pages = {}
    for visible in (False, True):
        for tail in (False, True):
            pages[f"visible={visible},tail={tail}"] = await _read_workspace_turn_log(
                workspace=env.workspace, relative_path=env.slot_rel, log=session_log_for(env.workspace, env.sid), limit=7, offset=0, since_seq=None,
                tail=tail, visible=visible,
                dedupe_legacy_user_input=True, fallback_created_at=FIXED_TS, workspace_id=WS, session_id=env.sid,
            )
    return pages


async def r_timeline_parse(env: Env) -> Any:
    """What the timeline route derives from the log's lines (the route reads them with ``_read_log_bytes`` over the log facade)."""
    from primer.api.routers.sessions import _read_log_bytes
    from primer.session.timeline import build_turn_timeline_from, parse_sources
    from primer.workspace.segments import session_log_for

    raw = await _read_log_bytes(env.workspace, env.slot_rel, workspace_id=WS, session_id=env.sid, log=session_log_for(env.workspace, env.sid))
    sources = parse_sources(raw.decode("utf-8", errors="replace").splitlines(), [])
    return [build_turn_timeline_from(sources, n) for n in range(5)]


async def r_route_text(env: Env) -> Any:
    """The bytes the manual compaction and rewind routes read (``workspace.read_file`` of the log), as a digest and the history they fold."""
    from primer.workspace.session import reconstruct_compacted_history

    from primer.workspace.segments import session_log_for

    log = session_log_for(env.workspace, env.sid)
    raw = await log.read_all()                                     # the rewind route's read
    text = (await log.since_last_marker())[1].decode("utf-8", errors="replace")   # the compaction route's read
    return {"sha": hashlib.sha256(raw).hexdigest()[:16], "history": _msgs(reconstruct_compacted_history(text.splitlines()), env.sid)}


async def r_final_text(env: Env) -> Any:
    from primer.channel.session_relay import read_session_final_text

    return await read_session_final_text(env.workspace, env.sid)


async def r_open_turn(env: Env) -> Any:
    from primer.session.dispatch import _read_message_lines
    from primer.session.turns import has_open_turn

    out = {}
    for cursor in (0, 4, 9, 14):
        row = env.row.model_copy(update={"next_unprocessed_seq": cursor})
        lines = await _read_message_lines(env.shim, row)
        out[str(cursor)] = None if lines is None else has_open_turn(lines, cursor=cursor)
    return out


def _events(events: list[Any], sid: str) -> list[Any]:
    return [_norm(e.model_dump(mode="json", by_alias=True), sid) for e in events]


async def r_tap_since(env: Env) -> Any:
    from primer.tap.reader import read_session_since
    from primer.tap.selector import TapSelector

    from primer.workspace.segments import SessionLog, WorkspaceSource

    # the offsets are logical: the midpoint is found in the whole log, not in segment 0
    raw = await SessionLog(WorkspaceSource(env.workspace), f"{env.state_path}/sessions/{env.sid}").read_all()
    mid = next((b for b in range(len(raw) // 2, len(raw)) if raw[b - 1 : b] == b"\n"), 0)
    out = {}
    for name, offset, after in (("start", 0, 0), ("mid", mid, 0), ("after_seq", 0, 9)):
        events, nxt = await read_session_since(
            env.workspace, workspace_id=WS, session=env.row, after_seq=after, selector=TapSelector(), from_offset=offset,
        )
        out[name] = {"events": _events(events, env.sid), "next_offset": nxt}
    return out


async def r_tap_batch(env: Env) -> Any:
    from primer.tap.cursor import TapCursor
    from primer.tap.reader import read_batch
    from primer.tap.selector import TapSelector

    out = {}
    for limit in (3, 1000):
        cursor = TapCursor(seqs={}, known_as_of=_WHEN)
        events, cursor = await read_batch(
            env.provider, env.workspace, workspace_id=WS, selector=TapSelector(), cursor=cursor, limit=limit,
        )
        out[str(limit)] = {"events": _events(events, env.sid), "seqs": cursor.seqs, "offsets": cursor.offsets}
    return out


async def r_record_by_seq(env: Env) -> Any:
    from primer.tap.reader import read_record_by_seq

    out = {}
    for seq in range(0, 20):
        record = await read_record_by_seq(env.workspace, session_id=env.sid, seq=seq)
        out[str(seq)] = None if record is None else [record.kind.value, record.seq]
    return out


async def r_memory_loader(env: Env) -> Any:
    from primer.memory.reflect import session_message_loader
    from primer.model.memory import MemoryJob

    load = session_message_loader(env.provider, env.shim.read_session_log)
    job = MemoryJob(id="j", kind="reflect", agent_id="ag-seg", session_id=env.sid, created_at=_WHEN, expires_at=_WHEN)
    messages = await load(job)
    return None if messages is None else _msgs(messages, env.sid)


READERS: dict[str, Callable[[Env], Awaitable[Any]]] = {
    "history": r_history,
    "compaction_state": r_compaction_state,
    "max_event_seq": r_max_event_seq,
    "take_pending": r_take_pending,
    "executor_history": r_executor_history,
    "usage_totals": r_usage_totals,
    "transcript_page": r_transcript_page,
    "timeline_parse": r_timeline_parse,
    "route_text": r_route_text,
    "final_text": r_final_text,
    "open_turn": r_open_turn,
    "tap_since": r_tap_since,
    "tap_batch": r_tap_batch,
    "record_by_seq": r_record_by_seq,
    "memory_loader": r_memory_loader,
}


async def run_readers(env: Env) -> dict[str, Any]:
    """Every reader's answer, JSON-normalised. A reader that raises is recorded as ``{"raised": "<type>"}`` (a segmented log may well break one)."""
    out: dict[str, Any] = {}
    for name, reader in READERS.items():
        try:
            out[name] = _norm(await reader(env), env.sid)
        except Exception as exc:  # noqa: BLE001 - the matrix compares answers, including failures
            out[name] = {"raised": type(exc).__name__}
    return out
