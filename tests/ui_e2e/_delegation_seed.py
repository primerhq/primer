"""A session transcript with a REAL delegated run, built by the same code that writes one.

Not hand-built dicts: the parent's records come from ``translate_stream_event`` (the persistence layer's own translation, which
writes the scoped call id in ``payload.id`` and the raw provider id in ``payload.raw_id``), the delegated records from the real
``DelegationRecorder`` (which stamps ``delegated``, ``delegate_tool_call_id`` and the run ids). The hand-built shape the console's
nesting was first tested with (``payload.tool_call_id``) is exactly how it never worked on real records.

The shape: a parent ``system__invoke_agent`` call (raw id ``call_0``) -> a child run (R1) that says something, makes its OWN call
reusing the raw id ``call_0`` (providers that synthesise ids restart the numbering every stream), and finishes -> a grandchild run
(R2, delegated to by the child's call, reusing ``call_0`` again) that says something. The parent then answers.

The subagent's own tool RESULTS are not here on purpose: the recorder translates stream events only, and a tool result is not one,
so the system never writes a delegated ``tool_result``.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from primer.model.chat import Done, TextDelta, ToolCallEnd, ToolCallStart
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.delegation import DelegationRecorder
from primer.session.persistence import _CoalesceState, translate_stream_event

USER_TEXT = "please delegate this to the helper"
CHILD_BEFORE = "child run: looking into it"
GRANDCHILD = "grandchild run: found the answer"
CHILD_AFTER = "child run: wrapping up"
PARENT_FINAL = "parent turn: all done"

RUN_CHILD = "11111111111111111111111111111111"
RUN_GRANDCHILD = "22222222222222222222222222222222"


@dataclass
class Seeded:
    records: list[dict[str, Any]]
    parent_call_seq: int
    child_call_seq: int
    delegated_assistant_seqs: list[int] = field(default_factory=list)
    top_level_assistant_seqs: list[int] = field(default_factory=list)


class _Writer:
    """What ``DelegationRecorder`` appends to: the same ordered log the parent turn's records go into."""

    def __init__(self) -> None:
        self.records: list[SessionMessageRecord] = []
        self._start = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

    def add(self, rec: SessionMessageRecord) -> int:
        seq = len(self.records) + 1
        self.records.append(rec.model_copy(update={"seq": seq, "created_at": self._start + timedelta(seconds=seq)}))
        return seq

    async def append(self, rec: SessionMessageRecord) -> int:
        return self.add(rec)


class _Bus:
    async def publish(self, key: str, payload: dict) -> None:
        return None


def _as_list(result) -> list[SessionMessageRecord]:
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def _run_to_completion(coro):
    """Run ``coro`` to completion, from a thread that may already have a running event loop.

    A Playwright test body runs inside pytest-playwright's loop, where ``asyncio.run`` raises (the journey's first CI run died
    on exactly that); a unit test or a plain script has no loop and can use it directly. With one running, the coroutine goes to
    a worker thread, which has none, and the caller waits for the result.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def build() -> Seeded:
    writer = _Writer()
    parent_state = _CoalesceState()

    def parent(event) -> list[int]:
        return [writer.add(r) for r in _as_list(translate_stream_event(event, parent_state, turn_no=1))]

    placeholder = datetime(2026, 10, 6, tzinfo=timezone.utc)  # overwritten by ``add``, which numbers and times the log
    writer.add(SessionMessageRecord(seq=1, kind=SessionMessageKind.USER_INPUT, payload={"text": USER_TEXT}, created_at=placeholder))
    parent(ToolCallStart(id="call_0", name="system__invoke_agent", index=0))
    (parent_call_seq,) = parent(ToolCallEnd(id="call_0", arguments={"agent_id": "helper", "prompt": "do it"}, index=0))
    parent_call = writer.records[parent_call_seq - 1]

    recorder = DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="seed", turn_no=1)

    async def delegated() -> int:
        child = {"delegate_tool_call_id": "call_0", "delegate_run_id": RUN_CHILD, "delegate_parent_run_id": None, "delegate_depth": 1}
        grandchild = {"delegate_tool_call_id": "call_0", "delegate_run_id": RUN_GRANDCHILD,
                      "delegate_parent_run_id": RUN_CHILD, "delegate_depth": 2}
        await recorder.on_event(TextDelta(index=0, text=CHILD_BEFORE), **child)
        await recorder.on_event(ToolCallStart(id="call_0", name="system__invoke_agent", index=0), **child)
        await recorder.on_event(ToolCallEnd(id="call_0", arguments={"agent_id": "grand"}, index=0), **child)
        child_call_seq = len(writer.records)
        await recorder.on_event(TextDelta(index=0, text=GRANDCHILD), **grandchild)
        await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), **grandchild)
        await recorder.on_event(TextDelta(index=0, text=CHILD_AFTER), **child)
        await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), **child)
        return child_call_seq

    child_call_seq = _run_to_completion(delegated())

    writer.add(SessionMessageRecord(
        seq=1, kind=SessionMessageKind.TOOL_RESULT, created_at=placeholder,
        payload={"call_id": parent_call.payload["id"], "output": "helper finished", "error": False},
    ))
    parent(TextDelta(index=0, text=PARENT_FINAL))
    parent(Done(stop_reason="stop", raw_reason="stop"))

    records = [r.model_dump(mode="json") for r in writer.records]
    delegated_assistant = [r["seq"] for r in records if r["kind"] == "assistant_token" and r["payload"].get("delegated")]
    top_assistant = [r["seq"] for r in records if r["kind"] == "assistant_token" and not r["payload"].get("delegated")]
    return Seeded(records, parent_call_seq, child_call_seq, delegated_assistant, top_assistant)
