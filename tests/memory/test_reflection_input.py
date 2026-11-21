"""Complete input coverage, locally bounded output and provider cleanup, all offline."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from primer.memory import reflection_input as ri
from primer.model.chat import Done, Error, Message, TextDelta, TextPart, ToolCallPart, ToolCallStart, ToolResultPart
from primer.model.memory import ReflectionOutput

SCHEMA = ReflectionOutput.model_json_schema()
MODEL = SimpleNamespace(context_length=32768, model_name="offline", config=None)


def messages(text):
    return [Message(role="user", parts=[TextPart(text=text)])]


def plan(text, **kwargs):
    return ri.plan_input(messages(text), 1, context="context", system="system", schema=SCHEMA,
                         window=kwargs.get("window", MODEL.context_length))


@pytest.mark.parametrize("text", ["hello", "修正" * 50, "🙂\\\"" * 30])
def test_direct_request_counts_system_schema_and_unicode(text):
    p = plan(text)
    assert p.direct is not None and p.chunks == ()
    assert ri.Budget.for_model(MODEL.context_length).fits("system", p.direct, SCHEMA)
    assert ri.request_size("system", p.direct, SCHEMA) > ri.text_tokens(text)


def test_long_single_part_fragments_reconstruct_every_character_including_middle():
    original = "a" * 70000 + "EARLY_ERROR_AND_CORRECTION" + "b" * 70000
    p = plan(original)
    assert p.direct is None and 1 < len(p.chunks) <= 4
    records = [json.loads(f.line) for chunk in p.chunks for f in chunk]
    assert "".join(row["text"] for row in records) == original
    assert len({row["source"] for row in records}) == len(records)
    for chunk in p.chunks:
        assert ri.Budget.for_model(MODEL.context_length, summary=True).fits(
            ri.MEMORY_SUMMARY_PROMPT, ri.summary_input(chunk), ri.EvidenceSummary.model_json_schema())


def test_tool_arguments_and_results_keep_complete_content_and_association():
    arguments = {"unicode": "修正" * 1000, "path": "nested/file"}
    output = "before " * 1000 + "MID_RESULT_FAILURE" + " after" * 1000
    source = [Message(role="assistant", parts=[ToolCallPart(id="call1", name="tool", arguments=arguments)]),
              Message(role="tool", parts=[ToolResultPart(id="call1", output=output, error=True)])]
    rows = [json.loads(f.line) for f in ri.fragments(source, 3)]
    calls = [r for r in rows if r["role"] == "assistant"]
    results = [r for r in rows if r["role"] == "tool"]
    assert json.loads("".join(r["text"] for r in calls)) == arguments
    assert "".join(r["text"] for r in results) == output
    assert all(r["call_id"] == "call1" for r in rows)
    assert all(r["tool"] == "tool" for r in calls) and all(r["error"] for r in results)


async def test_event_limit_closes_nontext_stream(monkeypatch):
    from primer.model.chat import ReasoningDelta
    monkeypatch.setattr(ri, "MAX_EVENTS", 2)
    llm = Stream([ReasoningDelta(text="", index=0) for _ in range(3)])
    with pytest.raises(ri.InputRefused, match="compaction_output_limit"):
        await judge(llm)
    assert llm.closed


@pytest.mark.parametrize("kind", ["policy", "range", "tiny"])
def test_uncoverable_plan_refuses_before_model_work(kind):
    with pytest.raises(ri.InputRefused):
        if kind == "policy":
            ri.plan_input(messages("hello"), 1, context="x" * 150000, system="system", schema=SCHEMA, window=32768)
        elif kind == "range":
            plan("x" * 600000)
        else:
            plan("hello", window=1000)


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.calls = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        try:
            for event in self.events:
                yield event
        finally:
            self.closed = True


async def judge(llm, **kwargs):
    return await ri.bounded_judge(llm, MODEL, system="system", user="source", schema=SCHEMA,
                                  budget=ri.Budget.for_model(MODEL.context_length), **kwargs)


async def test_valid_structured_stream_is_tool_free_and_closed():
    llm = Stream([TextDelta(text='{"run_succeeded":true,"deltas":[]}', index=0), Done(stop_reason="stop", raw_reason="stop")])
    assert (await judge(llm))["deltas"] == []
    assert llm.closed and len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None and llm.calls[0]["tool_choice"] is None
    assert llm.calls[0]["max_output_tokens"] == 4096


@pytest.mark.parametrize("kind", ["bytes", "tool", "invalid", "context"])
async def test_rejected_output_closes_stream(kind):
    event = TextDelta(text="x" * (ri.MAX_OUTPUT_BYTES + 1) if kind == "bytes" else "not json", index=0)
    if kind == "tool":
        event = ToolCallStart(id="unexpected", name="do_not_execute", index=0)
    if kind == "context":
        event = Error(code="context_length_exceeded", message="input too long", fatal=True)
    llm = Stream([event, Done(stop_reason="stop", raw_reason="stop")])
    with pytest.raises(ri.InvalidVerdict if kind == "invalid" else ri.InputRefused):
        await judge(llm)
    assert llm.closed


async def test_cancellation_waits_for_stream_cleanup_and_propagates():
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Held:
        async def stream(self, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield TextDelta(text="", index=0)
            finally:
                cleaning.set()
                await release.wait()
    task = asyncio.create_task(judge(Held()))
    await entered.wait()
    task.cancel()
    await cleaning.wait()
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("foreign", [False, True])
async def test_prepared_summary_carries_early_evidence_or_refuses_foreign_reference(foreign):
    p = plan("EARLY_CORRECTION " + "x" * 140000)
    seen = []
    class Summarizer:
        async def stream(self, **kwargs):
            user = kwargs["messages"][1].parts[0].text
            rows = [json.loads(line) for line in user.splitlines()[1:]]
            seen.extend(row["text"] for row in rows)
            facts = []
            if any("EARLY_CORRECTION" in row["text"] for row in rows):
                facts = [{"observation": "EARLY_CORRECTION observed", "sources": ["foreign" if foreign else rows[0]["source"]]}]
            yield TextDelta(text=json.dumps({"facts": facts}), index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
    async def check():
        pass
    work = ri.prepare_input(p, llm=Summarizer(), model=MODEL, context="context", system="system", schema=SCHEMA, check=check)
    if foreign:
        with pytest.raises(ri.InputRefused, match="compaction_invalid"):
            await work
    else:
        user, meta = await work
        assert "EARLY_CORRECTION observed" in user and "lossy evidence summary" in user
        assert "".join(seen) == "EARLY_CORRECTION " + "x" * 140000
        assert meta["summary_calls"] == len(p.chunks) <= ri.MAX_CHUNKS
        assert meta["input_estimate"] <= meta["input_budget"]


async def test_shared_deadline_cancels_owned_stream_and_closes_it():
    finished = asyncio.Event()
    class Slow:
        async def stream(self, **kwargs):
            try:
                await asyncio.Event().wait()
                yield TextDelta(text="", index=0)
            finally:
                finished.set()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.01):
            await judge(Slow())
    assert finished.is_set()


@pytest.mark.parametrize("failure", ["finally", "aclose"])
async def test_cleanup_failure_cannot_replace_repeated_outward_cancellation(failure):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def fail_close():
        cleaning.set()
        await release.wait()
        raise RuntimeError("isolated cleanup failure")
    class Iterator:
        def __aiter__(self):
            return self
        async def __anext__(self):
            entered.set()
            await asyncio.Event().wait()
        async def aclose(self):
            await fail_close()
    class Failing:
        def stream(self, **kwargs):
            if failure == "aclose":
                return Iterator()
            async def generate():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield TextDelta(text="", index=0)
                finally:
                    await fail_close()
            return generate()
    task = asyncio.create_task(judge(Failing()))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            task.cancel()
            await cleaning.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("wrong", ["identity", "repeat", "oversize"])
async def test_synthetic_result_remains_single_correlated_bounded_data(wrong):
    from primer.model.chat import ToolCallDelta, ToolCallEnd
    events = [ToolCallStart(id="result", name="structured_output", index=0)]
    if wrong == "identity":
        events.append(ToolCallEnd(id="other", arguments={}, index=0))
    elif wrong == "repeat":
        events += [ToolCallEnd(id="result", arguments={}, index=0),
                   ToolCallStart(id="second", name="structured_output", index=1)]
    else:
        events.append(ToolCallDelta(id="result", index=0, arguments_delta="x" * 100))
    llm = Stream(events)
    with pytest.raises(ri.InputRefused, match="compaction_output_limit" if wrong == "oversize" else "compaction_invalid"):
        await judge(llm, max_bytes=64)
    assert llm.closed


@pytest.mark.parametrize("reason", ["content_filter", "stop_sequence", "other", "tool_turn_cap", "tool_use"])
async def test_non_success_terminal_cannot_authorize_a_text_verdict(reason):
    llm = Stream([TextDelta(text='{"run_succeeded":true,"deltas":[]}', index=0),
                  Done(stop_reason=reason, raw_reason=reason)])
    with pytest.raises(ri.InputRefused, match="compaction_incomplete"):
        await judge(llm)
    assert llm.closed


async def test_success_terminal_does_not_complete_an_unfinished_synthetic_block():
    llm = Stream([ToolCallStart(id="result", name="structured_output", index=0),
                  Done(stop_reason="stop", raw_reason="stop")])
    with pytest.raises(ri.InputRefused, match="compaction_incomplete"):
        await judge(llm)
    assert llm.closed
