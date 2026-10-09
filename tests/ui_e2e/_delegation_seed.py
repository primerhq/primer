"""A session transcript with a REAL delegated run, built by the same code that writes one.

Not hand-built dicts: the parent's records come from ``translate_stream_event`` (the persistence layer's own translation, which
writes the scoped call id in ``payload.id`` and the raw provider id in ``payload.raw_id``), the delegated records from the real
``DelegationRecorder`` (which stamps ``delegated``, ``delegate_tool_call_id`` and the run ids). The hand-built shape the console's
nesting was first tested with (``payload.tool_call_id``) is exactly how it never worked on real records.

The shape: a parent ``system__invoke_agent`` call (raw id ``call_0``) -> a child run (R1) that says something, makes its OWN call
reusing the raw id ``call_0`` (providers that synthesise ids restart the numbering every stream), and finishes -> a grandchild run
(R2, delegated to by the child's call, reusing ``call_0`` again) that says something. The parent then answers.

The subagent's own tool RESULTS are here too: ``run_agent_turn`` yields the ``_ExecutorToolResult`` of a subagent's call, ``run_subagent`` feeds it to the
recorder, and ``translate_stream_event`` writes it as a ``tool_result`` stamped ``delegated`` with the run's ids (and the scoped call id the run's OWN coalescing state
numbered, which restarts per run: a child's call and its parent's are both ``x:tool:1:1``). A reader that pairs a call with its result by the scoped id alone pairs
a nested call with its PARENT's result; the run is part of the key. The result's output is the tool's own text, so a failed ``invoke_agent`` quotes the stream error
(``primer/toolset/system.py``: ``subagent 'x' LLM stream failed: <message>``).
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from primer.model.chat import Done, Error, ExtendedEvent, TextDelta, ToolCallEnd, ToolCallStart, _ExecutorToolResult
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.delegation import DelegationRecorder
from primer.session.persistence import _CoalesceState, translate_stream_event

USER_TEXT = "please delegate this to the helper"
CHILD_BEFORE = "child run: looking into it"
GRANDCHILD = "grandchild run: found the answer"
CHILD_AFTER = "child run: wrapping up"
PARENT_FINAL = "parent turn: all done"
# ``build(failures=True)``: the grandchild run fails for good (a fatal Error, as most providers end a failed stream), and the parent makes a SECOND call whose run
# ends the way the only producer of a non-fatal Error (the OpenResponses stream) ends it: the agent loop holds the Error, yields the Done first and the Error last,
# then raises, so the call is answered with an ERROR result and the delegated scope never gets a fatal record of its own.
GRANDCHILD_FAILURE = "grandchild run: the model fell over"
FAILED_TEXT = "flaky helper: working on it"
CHILD_NOTICE = "flaky helper: provider hiccup"

# What a failed ``invoke_agent`` answers (``_err`` in primer/toolset/_helpers.py: a ``{type, message}`` JSON text), quoting the subagent's stream error.
def failed_invoke_output(agent: str, message: str) -> str:
    return json.dumps({"type": "provider-error", "message": f"subagent {agent!r} LLM stream failed: {message}"})


RUN_CHILD = "11111111111111111111111111111111"
RUN_GRANDCHILD = "22222222222222222222222222222222"
RUN_FLAKY = "33333333333333333333333333333333"


@dataclass
class Seeded:
    records: list[dict[str, Any]]
    parent_call_seq: int
    child_call_seq: int
    delegated_assistant_seqs: list[int] = field(default_factory=list)
    top_level_assistant_seqs: list[int] = field(default_factory=list)
    delegated_failure_seq: int | None = None
    delegated_notice_seq: int | None = None
    failed_call_seq: int | None = None


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


def build(failures: bool = False) -> Seeded:
    """The seeded session; with ``failures`` the grandchild run ends in a fatal Error and a second call's run ends the way the OpenResponses stream does."""
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
        if failures:
            # No text before the failure, to keep this seed's rows as the journeys count them. The recorder keeps a coalescing buffer PER RUN and a fatal Error flushes the run's
            # buffered text ahead of its ERROR record (#578), so a subagent that streamed a partial answer and then failed would show that text in its own block; the recorder's
            # own tests (tests/session/test_delegation_recorder_failed_run.py, tests/agent/test_failed_subagent_keeps_its_text.py) cover it, no journey draws it.
            await recorder.on_event(Error(message=GRANDCHILD_FAILURE, code="server_error", fatal=True), **grandchild)
        else:
            await recorder.on_event(TextDelta(index=0, text=GRANDCHILD), **grandchild)
            await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), **grandchild)
        # The child's call to the grandchild is answered, and the recorder writes that result as the CHILD run's own (error when the run failed).
        child_output = failed_invoke_output("grand", GRANDCHILD_FAILURE) if failures else json.dumps({"output": GRANDCHILD})
        await recorder.on_event(ExtendedEvent(extended=_ExecutorToolResult(call_id="call_0", output=child_output, error=failures)), **child)
        await recorder.on_event(TextDelta(index=0, text=CHILD_AFTER), **child)
        await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), **child)
        return child_call_seq

    child_call_seq = _run_to_completion(delegated())

    writer.add(SessionMessageRecord(
        seq=1, kind=SessionMessageKind.TOOL_RESULT, created_at=placeholder,
        payload={"call_id": parent_call.payload["id"], "output": "helper finished", "error": False},
    ))
    failed_call_seq = None
    if failures:
        parent(ToolCallStart(id="call_1", name="system__invoke_agent", index=1))
        (failed_call_seq,) = parent(ToolCallEnd(id="call_1", arguments={"agent_id": "flaky", "prompt": "try"}, index=1))
        failed_call = writer.records[failed_call_seq - 1]

        async def flaky() -> None:
            run = {"delegate_tool_call_id": "call_1", "delegate_run_id": RUN_FLAKY, "delegate_parent_run_id": None, "delegate_depth": 1}
            await recorder.on_event(TextDelta(index=0, text=FAILED_TEXT), **run)
            # The real loop's order for [Error(fatal=False), Done(error)]: the Done first, the held Error last, then it raises.
            await recorder.on_event(Done(stop_reason="error", raw_reason="failed"), **run)
            await recorder.on_event(Error(message=CHILD_NOTICE, code="provider_warning", fatal=False), **run)

        _run_to_completion(flaky())
        writer.add(SessionMessageRecord(
            seq=1, kind=SessionMessageKind.TOOL_RESULT, created_at=placeholder,
            payload={"call_id": failed_call.payload["id"], "output": failed_invoke_output("flaky", CHILD_NOTICE), "error": True},
        ))
    parent(TextDelta(index=0, text=PARENT_FINAL))
    parent(Done(stop_reason="stop", raw_reason="stop"))

    records = [r.model_dump(mode="json") for r in writer.records]
    delegated_assistant = [r["seq"] for r in records if r["kind"] == "assistant_token" and r["payload"].get("delegated")]
    top_assistant = [r["seq"] for r in records if r["kind"] == "assistant_token" and not r["payload"].get("delegated")]
    failure_seq = next((r["seq"] for r in records if r["kind"] == "error" and r["payload"].get("fatal") is True), None)
    notice_seq = next((r["seq"] for r in records if r["kind"] == "error" and r["payload"].get("fatal") is False), None)
    return Seeded(records, parent_call_seq, child_call_seq, delegated_assistant, top_assistant, failure_seq, notice_seq, failed_call_seq)


GRAND_PARTIAL = "grand: partial"
GRAND_NOTICE = "grand: provider hiccup"
HELPER_AFTER = "helper: grand failed, did it myself"


def build_nested_notice(helper_calls_something_first: bool = False) -> Seeded:
    """A GRANDCHILD run that ends the way the OpenResponses stream ends it (the Done first, the held non-fatal Error last, then the loop raises).

    parent turn -> a ``system__invoke_agent`` call (raw id ``call_0``) -> the helper run (R1), which calls ``system__invoke_agent`` again (raw id ``call_0`` again, as a
    provider that numbers per stream does) -> the grand run (R2): text, ``Done(error)``, the notice. The helper's call is answered with an ERROR result that quotes the
    notice (written by the recorder, as the helper's own), the helper says its piece, and the parent's call is answered with an OK result: a PARENT result that shares
    its scoped id with the helper's call. With ``helper_calls_something_first`` the helper makes a successful ``workspace__read_file`` call before, so its failing
    call is its SECOND (scoped id ``x:tool:1:2``, which no parent id shares): a reader that pairs by id alone finds no result for it and calls the notice "continuing".

    ``Seeded.child_call_seq`` is the helper's call to grand (the call that FAILED), ``delegated_notice_seq`` grand's notice, ``parent_call_seq`` the parent's call.
    """
    writer = _Writer()
    parent_state = _CoalesceState()

    def parent(event) -> list[int]:
        return [writer.add(r) for r in _as_list(translate_stream_event(event, parent_state, turn_no=1))]

    placeholder = datetime(2026, 10, 6, tzinfo=timezone.utc)
    writer.add(SessionMessageRecord(seq=1, kind=SessionMessageKind.USER_INPUT, payload={"text": USER_TEXT}, created_at=placeholder))
    parent(ToolCallStart(id="call_0", name="system__invoke_agent", index=0))
    (parent_call_seq,) = parent(ToolCallEnd(id="call_0", arguments={"agent_id": "helper", "prompt": "do it"}, index=0))
    parent(Done(stop_reason="tool_use", raw_reason="tool_use"))
    recorder = DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="seed", turn_no=1)
    helper = {"delegate_tool_call_id": "call_0", "delegate_run_id": RUN_CHILD, "delegate_parent_run_id": None, "delegate_depth": 1}
    grand = {"delegate_tool_call_id": "call_0", "delegate_run_id": RUN_GRANDCHILD, "delegate_parent_run_id": RUN_CHILD, "delegate_depth": 2}
    seqs: dict[str, int] = {}

    async def delegated() -> None:
        if helper_calls_something_first:
            await recorder.on_event(ToolCallStart(id="call_0", name="workspace__read_file", index=0), **helper)
            await recorder.on_event(ToolCallEnd(id="call_0", arguments={"path": "a.txt"}, index=0), **helper)
            await recorder.on_event(Done(stop_reason="tool_use", raw_reason="tool_use"), **helper)
            await recorder.on_event(ExtendedEvent(extended=_ExecutorToolResult(call_id="call_0", output="hello", error=False)), **helper)
        await recorder.on_event(ToolCallStart(id="call_0", name="system__invoke_agent", index=0), **helper)
        await recorder.on_event(ToolCallEnd(id="call_0", arguments={"agent_id": "grand", "prompt": "sub"}, index=0), **helper)
        seqs["helper_call"] = len(writer.records)
        await recorder.on_event(Done(stop_reason="tool_use", raw_reason="tool_use"), **helper)
        await recorder.on_event(TextDelta(index=0, text=GRAND_PARTIAL), **grand)
        await recorder.on_event(Done(stop_reason="error", raw_reason="failed"), **grand)
        await recorder.on_event(Error(message=GRAND_NOTICE, code="provider_warning", fatal=False), **grand)
        seqs["notice"] = len(writer.records)
        await recorder.on_event(
            ExtendedEvent(extended=_ExecutorToolResult(call_id="call_0", output=failed_invoke_output("grand", GRAND_NOTICE), error=True)), **helper)
        await recorder.on_event(TextDelta(index=0, text=HELPER_AFTER), **helper)
        await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), **helper)

    _run_to_completion(delegated())
    parent(ExtendedEvent(extended=_ExecutorToolResult(call_id="call_0", output=json.dumps({"output": "helper finished"}), error=False)))
    parent(TextDelta(index=0, text=PARENT_FINAL))
    parent(Done(stop_reason="stop", raw_reason="stop"))
    records = [r.model_dump(mode="json") for r in writer.records]
    return Seeded(records, parent_call_seq, seqs["helper_call"], delegated_notice_seq=seqs["notice"])


ANSWER_A = "node A's helper: the answer is A"
ANSWER_B = "node B's helper: the answer is B"
RUN_FAN_A = "44444444444444444444444444444444"
RUN_FAN_B = "55555555555555555555555555555555"


@dataclass
class SeededFanout:
    records: list[dict[str, Any]]
    call_a_seq: int
    call_b_seq: int


def build_fanout() -> SeededFanout:
    """Two concurrent fan-out siblings of a graph (nodes ``A`` and ``B``) that each delegate to a helper under the SAME raw call id ``call_0``.

    Providers that synthesise call ids restart the numbering every stream, so both nodes' ``invoke_agent`` calls are ``call_0``. Both calls are written before either run's
    records (the nodes run at once), and the runs' records are interleaved, as concurrent siblings write them. The records come from ``translate_stream_event`` (the parent's call
    rows carry their node) and the real ``DelegationRecorder`` (the delegated records carry ``delegate_node_id``, the node that delegated).
    """
    writer = _Writer()
    parent_state = _CoalesceState()

    def parent(event, node: str) -> list[int]:
        return [writer.add(r) for r in _as_list(translate_stream_event(event, parent_state, turn_no=1, node_id=node))]

    placeholder = datetime(2026, 10, 6, tzinfo=timezone.utc)
    writer.add(SessionMessageRecord(seq=1, kind=SessionMessageKind.USER_INPUT, payload={"text": USER_TEXT}, created_at=placeholder))
    seqs: dict[str, int] = {}
    for node in ("A", "B"):
        parent(ToolCallStart(id="call_0", name="system__invoke_agent", index=0), node)
        (seqs[node],) = parent(ToolCallEnd(id="call_0", arguments={"agent_id": "helper", "prompt": f"for {node}"}, index=0), node)

    recorder = DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="seed", turn_no=1)

    async def delegated() -> None:
        a = {"delegate_tool_call_id": "call_0", "delegate_run_id": RUN_FAN_A, "delegate_parent_run_id": None, "delegate_depth": 1, "delegate_node_id": "A"}
        b = {"delegate_tool_call_id": "call_0", "delegate_run_id": RUN_FAN_B, "delegate_parent_run_id": None, "delegate_depth": 1, "delegate_node_id": "B"}
        await recorder.on_event(TextDelta(index=0, text=ANSWER_B), **b)
        await recorder.on_event(TextDelta(index=0, text=ANSWER_A), **a)
        await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), **b)
        await recorder.on_event(Done(stop_reason="stop", raw_reason="stop"), **a)

    _run_to_completion(delegated())
    for node in ("A", "B"):
        call = writer.records[seqs[node] - 1]
        writer.add(SessionMessageRecord(
            seq=1, kind=SessionMessageKind.TOOL_RESULT, created_at=placeholder,
            payload={"call_id": call.payload["id"], "output": f"helper {node} finished", "error": False},
        ))
    parent(TextDelta(index=0, text=PARENT_FINAL), "A")
    parent(Done(stop_reason="stop", raw_reason="stop"), "A")
    return SeededFanout([r.model_dump(mode="json") for r in writer.records], seqs["A"], seqs["B"])
