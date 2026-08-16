"""C1: subagent events append prompt-excluded records to the parent log.

Spec: docs/superpowers/ux-revamp/02-s1-design.md, amendment C1.
Plan: S1 P1 Task 7.

run_subagent executes INSIDE the delegating turn and owns no writer, so
a delegated run was invisible in the transcript. The dispatch publishes
a recorder through a contextvar and the invoke loop feeds it every
subagent event, which is what gives S7 its trace coverage and S8 its
nesting anchor (both keyed on payload["delegate_tool_call_id"]).
"""

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
