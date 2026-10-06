"""C1: subagent events append prompt-excluded records to the parent log.

Spec: docs/superpowers/ux-revamp/02-s1-design.md, amendment C1.
Plan: S1 P1 Task 7.

run_subagent executes INSIDE the delegating turn and owns no writer, so
a delegated run was invisible in the transcript. The dispatch publishes
a recorder through a contextvar and the invoke loop feeds it every
subagent event, which is what gives S7 its trace coverage and S8 its
nesting anchor (both keyed on payload["delegate_tool_call_id"]).
"""

import asyncio

from primer.agent.call_scope import CallScope, bind_call_scope
from primer.model.chat import Done, TextDelta
from primer.session.delegation import (
    DelegationRecorder,
    current_delegation_sink,
    reset_delegation_sink,
    set_delegation_sink,
)


class _Writer:
    def __init__(self):
        self.records = []
        self._seq = 0

    async def append(self, rec):
        self._seq += 1
        self.records.append(rec)
        return self._seq


class _Bus:
    def __init__(self):
        self.published = []

    async def publish(self, key, payload):
        self.published.append((key, payload))


async def test_subagent_events_become_delegated_records():
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    await rec.on_event(TextDelta(index=0, text="thinking about it"),
                       delegate_tool_call_id="call_7")
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_7")

    kinds = [r.kind.value for r in w.records]
    assert "assistant_token" in kinds
    assert "done" in kinds
    for r in w.records:
        assert r.payload["delegated"] is True
        assert r.payload["delegate_tool_call_id"] == "call_7"
    assert b.published
    assert b.published[0][0] == "session:s:tick"


async def test_the_run_id_the_parent_run_and_the_depth_are_stamped_when_given():
    """Raw call ids are not unique (a child's own call can reuse its parent's), so what tells two runs apart is a run id minted
    when the run starts, with the id of the run whose call delegated to it and the nesting depth."""
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    await rec.on_event(
        Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_0",
        delegate_run_id="run-2", delegate_parent_run_id="run-1", delegate_depth=2,
    )
    (r,) = w.records
    assert r.payload["delegate_tool_call_id"] == "call_0"
    assert r.payload["delegate_run_id"] == "run-2"
    assert r.payload["delegate_parent_run_id"] == "run-1"
    assert r.payload["delegate_depth"] == 2


async def test_nothing_is_stamped_for_what_is_not_given():
    """A direct delegation of the parent turn has no parent run; a caller that predates the ids passes none of them."""
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_0", delegate_run_id="run-1", delegate_depth=1)
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_0")
    direct, legacy = (r.payload for r in w.records)
    assert "delegate_parent_run_id" not in direct and direct["delegate_run_id"] == "run-1"
    assert not {"delegate_run_id", "delegate_parent_run_id", "delegate_depth"} & set(legacy)


async def test_records_are_event_log_lines_not_llm_history():
    """The stamps ride SessionMessageRecords, which the history reader
    never admits: only role/parts Message lines rebuild a prompt. That
    is what makes delegated output visible without replaying a
    subagent's chatter back into the parent's next turn."""
    from primer.workspace.session import reconstruct_compacted_history

    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    await rec.on_event(TextDelta(index=0, text="inner"), delegate_tool_call_id="c1")
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="c1")

    lines = [r.model_dump_json() for r in w.records]
    assert reconstruct_compacted_history(lines) == []


def test_contextvar_set_and_reset():
    assert current_delegation_sink() is None
    marker = object()
    token = set_delegation_sink(marker)
    assert current_delegation_sink() is marker
    reset_delegation_sink(token)
    assert current_delegation_sink() is None


async def test_untranslatable_events_are_dropped_quietly():
    """Most stream events produce no record; the recorder must not
    invent one or crash the delegating turn."""
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    await rec.on_event(TextDelta(index=0, text="buffered"), delegate_tool_call_id="c")
    assert w.records == []  # text coalesces until a flush point
    assert b.published == []


async def _in_a_call(scope: CallScope, work):
    """Run ``work()`` the way a tool call runs: in its own task, with the call's scope bound in that task's context."""

    async def run():
        bind_call_scope(scope)
        return await work()

    return await asyncio.create_task(run())


async def test_an_abandoned_call_records_nothing_more():
    """Stop slice B1: when a Stop gives up on a subagent call, its eventual events must not be appended to the parent log
    AFTER the synthetic result that answered the call (they would read as the subagent continuing past the Stop)."""
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    scope = CallScope()
    counts: dict[str, int] = {}

    async def work():
        await rec.on_event(TextDelta(index=0, text="before"), delegate_tool_call_id="call_7")
        await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_7")
        counts["before"], counts["published"] = len(w.records), len(b.published)
        scope.abandon()
        await rec.on_event(TextDelta(index=0, text="after"), delegate_tool_call_id="call_7")
        await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_7")

    await _in_a_call(scope, work)

    assert counts["before"] > 0
    assert len(w.records) == counts["before"], "an abandoned call kept appending to the parent log"
    assert len(b.published) == counts["published"], "an abandoned call kept publishing ticks"


async def test_events_of_a_nested_call_are_dropped_whatever_id_they_carry():
    """A subagent that delegated tags its events with the INNER call's id. Abandonment follows the call's scope (the task
    tree), not one id, so those are dropped too."""
    w = _Writer()
    rec = DelegationRecorder(writer=w, event_bus=_Bus(), session_id="s")
    scope = CallScope()
    scope.abandon()

    async def work():
        await rec.on_event(TextDelta(index=0, text="inner"), delegate_tool_call_id="inner_call")
        await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="inner_call")

    await _in_a_call(scope, work)

    assert w.records == []


async def test_abandoning_one_call_leaves_the_others_recording():
    w, b = _Writer(), _Bus()
    rec = DelegationRecorder(writer=w, event_bus=b, session_id="s")
    abandoned, other = CallScope(), CallScope()
    abandoned.abandon()

    async def work():
        await rec.on_event(TextDelta(index=0, text="mine"), delegate_tool_call_id="call_b")
        await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_b")

    await _in_a_call(other, work)
    assert w.records, "an unrelated call stopped recording"
    assert {r.payload["delegate_tool_call_id"] for r in w.records} == {"call_b"}


async def test_a_turn_without_a_stop_has_no_scope_and_records_as_before():
    """The control: no call scope bound (every turn that is never stopped): nothing is dropped."""
    w = _Writer()
    rec = DelegationRecorder(writer=w, event_bus=_Bus(), session_id="s")

    await rec.on_event(TextDelta(index=0, text="x"), delegate_tool_call_id="call_7")
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_7")

    assert w.records


async def test_an_abandon_between_the_records_of_one_event_stops_the_rest():
    """One event can translate to several records; the abandon can land while the first is being written."""
    scope = CallScope()

    class _AbandoningWriter(_Writer):
        async def append(self, rec):
            seq = await super().append(rec)
            if len(self.records) == 1:
                scope.abandon()                        # the Stop gives up on the call mid-event
            return seq

    w = _AbandoningWriter()
    rec = DelegationRecorder(writer=w, event_bus=_Bus(), session_id="s")

    async def work():
        await rec.on_event(TextDelta(index=0, text="buffered"), delegate_tool_call_id="call_7")
        await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="call_7")

    await _in_a_call(scope, work)

    assert len(w.records) == 1, "records of an abandoned call were written after the abandon"
