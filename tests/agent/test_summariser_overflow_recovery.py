"""The compaction's own summariser call overflows: one bounded recovery, then a failure that names it (A.4, R8).

The summariser is a model call like any other and is sent the head the compaction replaces. A head over the
model's window used to fail the turn with the provider's 400 (the characterisation test in
``test_overflow_replay_characterisation`` pinned that). The recovery lives in ``_full_compact``, so the
proactive compaction, the forced one (after a turn overflow) and the manual route all get it:

* the call is made again TEXT ONLY (no tools: no tool runs twice, and the tool schemas stop counting) on an
  input reduced to what fits: tool results left out, then a bounded rolling fold, then a single unit cut;
* the marker records what was done (``summary_input_reduced``), and only when something overflowed;
* a head the bounded fold cannot hold, or a retry that overflows too, is :class:`SummariserOverflow`, which
  names the summariser; nothing is tried a second time;
* a tool-enabled summariser that overflows in a LATER round ends with the summary it already wrote and never
  starts its tool loop again.

These run the real ``WorkspaceAgentExecutor`` over a real local workspace; the model is a small window that
rejects any call over it, as a provider does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.prompts import DEFAULT_COMPACTION_PROMPT
from primer.agent.summary_input import SUMMARISER_CHUNK_FRACTION
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import (
    Done,
    Message,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    Tool,
    ToolResultPart,
)
from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable, RateLimitError, SummariserOverflow
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from tests._support.off_golden import (
    CONTEXT_LENGTH, append_messages, assistant_message, make_agent, make_model, open_session, user_message,
)

OVERFLOW = "This model's maximum context length is 100000 tokens, however you requested more"
LEFT_OUT = "left out of the summariser's input"
NEEDS_EXEC = pytest.mark.skipif(not Path("/usr/bin/env").exists(), reason="needs a POSIX shell for the exec tool")


# ------------------------------------------------------------------ a model with a window


@dataclass
class _Call:
    index: int
    messages: list[Message]
    tools: list[Any]
    tokens: int

    @property
    def is_summary(self) -> bool:
        first = self.messages[0]
        return first.role == "system" and first.parts[0].text == DEFAULT_COMPACTION_PROMPT  # type: ignore[union-attr]

    @property
    def text(self) -> str:
        return "\n".join(
            part.text if isinstance(part, TextPart) else part.output if isinstance(part, ToolResultPart) else ""
            for m in self.messages for part in m.parts
        )


@dataclass
class _Window:
    """A model with a context window. A call over ``window`` tokens (counted as the strategy counts, tool
    schemas included) is rejected as a context overflow, as the provider does; ``reject`` rejects the n-th
    summariser call whatever its size (the provider counted more than we did); ``counts`` scales our estimate
    into what the provider counts (a tokenizer denser than chars/4); ``script`` answers a call with events, an
    exception, or ``None`` for the default."""

    window: int = CONTEXT_LENGTH
    counts: float = 1.0
    reject_summaries: int = 0
    script: Any = None
    calls: list[_Call] = field(default_factory=list)
    summaries_seen: int = 0
    session_id: str = ""

    async def list_models(self) -> list[str]:
        return ["m"]

    def unused(self) -> int:
        return 0

    @property
    def summaries(self) -> list[_Call]:
        return [c for c in self.calls if c.is_summary]

    @property
    def turns(self) -> list[_Call]:
        return [c for c in self.calls if not c.is_summary]

    def stream(self, *, model, messages, **kwargs):
        tools = list(kwargs.get("tools") or [])
        tokens = int(self.counts * (CompactionStrategy._estimate_tokens(messages) + sum(len(t.model_dump_json()) // 4 for t in tools)))
        call = _Call(len(self.calls) + 1, list(messages), tools, tokens)
        self.calls.append(call)
        return self._run(call)

    async def _run(self, call: _Call):
        rejected = call.tokens > self.window
        if call.is_summary:
            self.summaries_seen += 1
            rejected = rejected or self.summaries_seen <= self.reject_summaries
        if rejected:
            raise BadRequestError(OVERFLOW)
        answer = self.script(call) if self.script is not None else None
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            answer = [TextDelta(text=f"SUMMARY {len(self.summaries)}" if call.is_summary else "done", index=0),
                      Done(stop_reason="stop", raw_reason="stop")]
        for event in answer:
            yield event


def _exec_round(command: str, *, text: str = "") -> list:
    events: list = [TextDelta(text=text, index=0)] if text else []
    return [
        *events,
        ToolCallStart(id="call_s", name="workspace__exec", index=1),
        ToolCallEnd(id="call_s", arguments={"command": command, "description": "count executions"}, index=1),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


# ------------------------------------------------------------------------------- seeding


async def _seed_rounds(workspace, session, *, turns: int, rounds: int, result_chars: int = 60_000) -> None:
    """``turns`` turns of ``rounds`` tool rounds each. A result of 60k characters is about 15k tokens: under
    the 20k a tier-1 prune would give back, so what the head weighs is what the summariser is sent."""
    for t in range(turns):
        await append_messages(workspace, session, user_message(f"question {t}"))
        for r in range(rounds):
            cid = f"call_{t}_{r}"
            await append_messages(
                workspace, session,
                Message(role="assistant", parts=[ToolCallPart(id=cid, name="exec", arguments={"cmd": f"cat part_{t}_{r}"})]),
                Message(role="tool", parts=[ToolResultPart(id=cid, output=chr(ord("a") + (t * rounds + r) % 26) * result_chars)]),
            )
        await append_messages(workspace, session, assistant_message(f"answer {t}"))
    await append_messages(workspace, session, user_message("now do the thing"))


async def _seed_text_units(workspace, session, *, units: int, chars: int) -> None:
    """``units`` user/assistant pairs, each user message ``chars`` long: text, which nothing can prune or omit."""
    for i in range(units):
        await append_messages(workspace, session, user_message(chr(ord("A") + i) + "x" * chars), assistant_message(f"reply {i}"))
    await append_messages(workspace, session, user_message("now do the thing"))


async def _seed_replies(workspace, session, n: int = 6, chars: int = 20_000) -> None:
    """A history under the trigger (5k tokens a turn), with a head before the tail: only a forced compaction touches it."""
    for i in range(n):
        await append_messages(workspace, session, user_message(f"filler {i}: " + "f" * chars), assistant_message(f"reply {i}"))
    await append_messages(workspace, session, user_message("now do the thing"))


# ----------------------------------------------------------------------------- the turn


def _executor(session, llm: _Window, *, tool_access: bool = False) -> WorkspaceAgentExecutor:
    agent = make_agent().model_copy(update={"compaction_tool_access": tool_access})
    return WorkspaceAgentExecutor(
        agent=agent, llm=llm,  # type: ignore[arg-type]
        llm_model=make_model(), tool_manager=ToolExecutionManager.for_workspace(toolset_providers={}, session=session),
        session=session, compaction=CompactionStrategy(),
    )


async def _turn(session, llm: _Window, *, tool_access: bool = False) -> list:
    llm.session_id = session.session_id
    return [event async for event in _executor(session, llm, tool_access=tool_access).invoke([])]


def _lines(workspace, session) -> list[dict]:
    path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _markers(workspace, session) -> list[dict]:
    return [line for line in _lines(workspace, session) if line.get("kind") == "compaction_marker"]


def _left_out(call: _Call) -> int:
    """How many tool results the call was sent with their output left out."""
    return sum(
        LEFT_OUT in part.output
        for m in call.messages for part in m.parts if isinstance(part, ToolResultPart)
    )


async def _with_session(tmp_path, body):
    backend, workspace, session = await open_session(tmp_path)
    try:
        return await body(workspace, session)
    finally:
        await session.aclose()
        await backend.aclose()


# ---------------------------------------------------------------- the recovery, step by step


async def test_tool_results_are_left_out_of_the_summarisers_input_and_the_turn_goes_on(tmp_path) -> None:
    async def body(workspace, session):
        # 60 results of 2.5k tokens: the head is over the window by a little, and omitting results one by
        # one lands close to wherever the target is, so the margin below can be seen
        await _seed_rounds(workspace, session, turns=15, rounds=4, result_chars=10_000)
        llm = _Window()
        await _turn(session, llm)

        first, retry = llm.summaries
        assert first.tokens > llm.window, "the head did not fit: that is what the provider rejected"
        assert retry.tokens <= llm.window and not retry.tools, "the retry fits and is text only"
        strategy = CompactionStrategy()
        assert retry.tokens <= 0.9 * (strategy._effective_budget(make_model()) - strategy.summary_max_tokens), \
            "and leaves the summary its room, with a margin: it is not trimmed to the last token"
        left_out = _left_out(retry)
        assert 0 < left_out < sum(
            isinstance(p, ToolResultPart) for m in first.messages for p in m.parts
        ), "results are omitted only until the head fits: the rest stay"
        assert llm.turns, "the turn itself ran, on the compacted history"

        (marker,) = _markers(workspace, session)
        assert marker["payload"]["summary_input_reduced"] == {"pruned": left_out, "folded_chunks": 0, "truncated_parts": 0}
        assert "SUMMARY 2" in marker["payload"]["summary"], "the summary is the retry's"
    await _with_session(tmp_path, body)


async def test_a_head_of_text_is_folded_in_chunks_that_each_fit(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_text_units(workspace, session, units=5, chars=100_000)          # 25k tokens each, 4 in the head
        llm = _Window()
        await _turn(session, llm)

        first, *fold = llm.summaries
        assert first.tokens > llm.window
        assert len(fold) == 4 and all(c.tokens <= llm.window and not c.tools for c in fold)
        assert "[the summary so far]" not in fold[0].text, "the first chunk starts the summary"
        for before, call in zip(fold, fold[1:]):
            index = fold.index(before) + 1
            assert f"[the summary so far]\n\nSUMMARY {index + 1}" in call.text, "each call carries the summary so far"
        sent = "".join(c.text for c in fold)
        assert all(sent.count(chr(ord("A") + i) + "x") == 1 for i in range(4)), "every unit of the head is read once"
        (marker,) = _markers(workspace, session)
        assert marker["payload"]["summary_input_reduced"] == {"pruned": 0, "folded_chunks": 4, "truncated_parts": 0}
        assert "SUMMARY 5" in marker["payload"]["summary"], "the summary is the last call's"
    await _with_session(tmp_path, body)


@pytest.mark.parametrize("window", [50_000, 40_000], ids=["above-a-chunk", "between-the-goal-and-a-chunk"])
async def test_one_unit_over_a_chunk_is_cut_head_and_tail_when_the_provider_counted_more_than_we_did(tmp_path, window) -> None:
    async def body(workspace, session):
        # A 60k-token unit: our estimate says it fits (the compaction was sized to), the window the provider
        # enforces is smaller, so the first call is rejected and the input must be cut to a chunk.
        await append_messages(workspace, session, user_message("H" + "m" * 240_000), assistant_message("noted"))
        await _seed_text_units(workspace, session, units=1, chars=100_000)            # the tail: the head is the big unit alone
        llm = _Window(window=window)
        await _turn(session, llm)

        first, retry = llm.summaries
        assert first.tokens > llm.window and retry.tokens <= llm.window
        unit = next(m.parts[0].text for m in retry.messages if m.role == "user" and m.parts[0].text.startswith("Hm"))
        chunk = int(SUMMARISER_CHUNK_FRACTION * CompactionStrategy()._effective_budget(make_model()))
        assert LEFT_OUT in unit and len(unit) // 4 <= chunk, "the unit's middle is gone: it was cut to a chunk"
        assert unit.startswith("Hmmm") and unit.endswith("mmm"), "its head and its tail stay"
        (marker,) = _markers(workspace, session)
        assert marker["payload"]["summary_input_reduced"]["truncated_parts"] == 1
        assert marker["payload"]["summary_input_reduced"]["folded_chunks"] == 0, "one chunk is one call"
    await _with_session(tmp_path, body)


async def test_a_head_the_bounded_fold_cannot_hold_fails_at_once_naming_the_summariser(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_text_units(workspace, session, units=7, chars=100_000)          # 6 in the head: 6 chunks, 4 allowed
        llm = _Window()
        with pytest.raises(SummariserOverflow, match="summariser") as failed:
            await _turn(session, llm)

        assert len(llm.summaries) == 1, "nothing was tried that cannot work: the one rejected call is the only call"
        assert not llm.turns and _markers(workspace, session) == []
        assert isinstance(failed.value, ContextOverflowUnrecoverable) and isinstance(failed.value.__cause__, BadRequestError)
        assert failed.value.ended_detail_code == failed.value.code == "summariser_overflow"
        assert "bounded at 4" in str(failed.value)
    await _with_session(tmp_path, body)


async def test_a_retry_that_overflows_too_fails_naming_the_summariser_and_is_not_repeated(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_rounds(workspace, session, turns=3, rounds=4)
        llm = _Window(reject_summaries=2)                                            # the reduced call is rejected too
        with pytest.raises(SummariserOverflow, match="again after its input was reduced"):
            await _turn(session, llm)

        assert len(llm.summaries) == 2, "one retry, then fail: no third call"
        assert not llm.turns and _markers(workspace, session) == []
    await _with_session(tmp_path, body)


async def test_a_summariser_that_fails_for_another_reason_is_not_retried(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_rounds(workspace, session, turns=3, rounds=4)
        llm = _Window(window=10**9, script=lambda call: RateLimitError("slow down") if call.is_summary else None)
        with pytest.raises(RateLimitError):
            await _turn(session, llm)
        assert len(llm.summaries) == 1
    await _with_session(tmp_path, body)


async def test_a_compaction_that_did_not_overflow_records_no_reduction(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_rounds(workspace, session, turns=3, rounds=4)
        llm = _Window(window=10**9)                                                  # nothing is ever rejected
        await _turn(session, llm)
        (marker,) = _markers(workspace, session)
        assert "summary_input_reduced" not in marker["payload"]
        assert len(llm.summaries) == 1
    await _with_session(tmp_path, body)


# ------------------------------------------------------------------- the forced compaction


async def test_a_forced_compaction_whose_summariser_overflows_recovers_the_same_way(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_replies(workspace, session)                                      # under the trigger: no proactive compaction

        def script(call: _Call):
            return BadRequestError(OVERFLOW) if not call.is_summary and not llm.summaries else None

        # the turn overflows, then the summariser: it is rejected whatever its size (the provider counted more
        # than we did), so the retry is a smaller input, here a fold of the head in two or more calls
        llm = _Window(reject_summaries=1, script=script)
        events = await _turn(session, llm)

        rejected, *retries = llm.summaries
        assert [c.is_summary for c in llm.calls] == [False, True, *[True] * len(retries), False]
        assert len(retries) >= 2 and all(c.tokens < rejected.tokens and not c.tools for c in retries), \
            "each retry call reads less than the one the provider rejected, and runs no tool"
        (marker,) = _markers(workspace, session)
        assert marker["payload"]["summary_input_reduced"] == {
            "pruned": 0, "folded_chunks": len(retries), "truncated_parts": 0,
        }
        assert "done" in "".join(e.text for e in events if isinstance(e, TextDelta))
    await _with_session(tmp_path, body)


# ----------------------------------------------- the retry is sized to the window, not to the first call


def _strategy_model(context_length: int) -> ResolvedModel:
    return ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m", context_length=context_length, config=ModelProfileConfig(),
    )


async def _compact(head: list[Message], llm: _Window, context_length: int):
    return await CompactionStrategy()._full_compact(  # noqa: SLF001
        head=head, agent=make_agent(), llm=llm, model=_strategy_model(context_length),  # type: ignore[arg-type]
    )


async def test_a_provider_that_counts_more_than_we_do_is_sent_a_smaller_input_not_the_same_one() -> None:
    """Our estimate says the 3 x 40k-character head (30k tokens) fits a 100k window; the provider counts four times
    that and rejected it. The retry must be SMALLER than what was rejected (a chunk the size of the head is the same
    input, rejected again), each call within what the provider holds."""
    head = [Message(role="user" if i % 2 == 0 else "assistant", parts=[TextPart(text="x" * 40_000)]) for i in range(3)]
    llm = _Window(counts=4.0, window=CONTEXT_LENGTH - 4_096)
    _, reduction = await _compact(head, llm, CONTEXT_LENGTH)

    rejected, *retries = llm.calls
    assert rejected.tokens > llm.window
    assert len(retries) >= 2 and all(c.tokens <= llm.window and c.tokens < rejected.tokens for c in retries)
    assert reduction is not None and reduction.folded_chunks == len(retries)


async def test_a_head_under_the_room_but_over_the_target_is_not_sent_back_at_97_percent() -> None:
    """78k tokens by our count (over the 76.5k target, under the 95.7k room) and a provider that counts 1.5x: the
    retry has to be a different, smaller input, not the head with one result left out."""
    head = [Message(role="user" if i % 2 == 0 else "assistant", parts=[TextPart(text="x" * 60_000)]) for i in range(5)]
    head += [
        Message(role="assistant", parts=[ToolCallPart(id="c", name="cat", arguments={})]),
        Message(role="tool", parts=[ToolResultPart(id="c", output="r" * 12_000)]),
        Message(role="assistant", parts=[TextPart(text="done")]),
    ]
    llm = _Window(counts=1.5, window=CONTEXT_LENGTH - 4_096)
    _, reduction = await _compact(head, llm, CONTEXT_LENGTH)

    rejected, *retries = llm.calls
    assert rejected.tokens > llm.window and len(retries) >= 2
    assert all(c.tokens <= llm.window and c.tokens < 0.7 * rejected.tokens for c in retries), [c.tokens for c in llm.calls]
    assert reduction is not None


class _Tools:
    """A tool manager with a big catalogue that must never run a tool."""

    def __init__(self, n: int, chars: int) -> None:
        self.catalogue = [
            Tool(id=f"t{i}", toolset_id="ts", description="d" * chars, args_schema={"type": "object", "properties": {}})
            for i in range(n)
        ]

    async def list_tools(self, *, principal=None):
        return self.catalogue

    async def execute(self, call, *, principal=None):
        raise AssertionError("the retry runs no tool")


async def test_an_overflow_the_tool_schemas_caused_is_retried_text_only_on_the_head_as_it_was() -> None:
    """The head (150 parallel small calls and results, about 15k tokens) fits a 64k window; the 50k tokens of tool
    schemas beside it did not. The text-only retry drops the schemas, so the head is sent unchanged: it must not be
    cut by the provider-counted-more margin, which a unit of many small parts cannot take."""
    ids = [f"c{i}" for i in range(150)]
    head = [
        Message(role="user", parts=[TextPart(text="check every host")]),
        Message(role="assistant", parts=[ToolCallPart(id=i, name="ping", arguments={"host": f"h{i}.example"}) for i in ids]),
        Message(role="tool", parts=[ToolResultPart(id=i, output="ok " * 30) for i in ids]),
        Message(role="assistant", parts=[TextPart(text="all hosts up")]),
    ]
    llm = _Window(window=65_536 - 4_096)
    _, reduction = await CompactionStrategy()._full_compact(  # noqa: SLF001
        head=head, agent=make_agent(), llm=llm, model=_strategy_model(65_536),  # type: ignore[arg-type]
        tool_manager=_Tools(100, 2_000),
    )

    first, retry = llm.calls
    assert first.tools and first.tokens > llm.window, "the catalogue is what made it too large"
    assert not retry.tools and retry.tokens < llm.window
    assert reduction is not None and reduction.as_payload() == {"pruned": 0, "folded_chunks": 0, "truncated_parts": 0}


class _BigResultTools(_Tools):
    """Every tool call answers with about 50k tokens: the results are what outgrow the window in round two."""

    async def execute(self, call, *, principal=None):
        return ToolResultPart(id=call.id, output="r" * 200_000)


async def test_a_tool_loop_that_overflows_in_round_two_with_no_text_yet_is_retried_on_the_head_as_it_was() -> None:
    """Round one only calls a tool (no summary text yet) and its result outgrows the window in round two. The head
    plus the schemas fitted in round one, so the overflow is explained by what the rounds carried: the text-only
    retry sends the head unchanged and must not demand the provider-counted-more cut, which a unit of many small
    parts cannot take."""
    ids = [f"c{i}" for i in range(150)]
    head = [
        Message(role="user", parts=[TextPart(text="check every host")]),
        Message(role="assistant", parts=[ToolCallPart(id=i, name="ping", arguments={"host": f"h{i}.example"}) for i in ids]),
        Message(role="tool", parts=[ToolResultPart(id=i, output="ok " * 30) for i in ids]),
        Message(role="assistant", parts=[TextPart(text="all hosts up")]),
    ]
    llm = _Window(window=65_536 - 4_096, script=lambda call: _exec_round("whatever") if call.is_summary and call.tools and len(llm.summaries) == 1 else None)
    _, reduction = await CompactionStrategy()._full_compact(  # noqa: SLF001
        head=head, agent=make_agent(), llm=llm, model=_strategy_model(65_536),  # type: ignore[arg-type]
        tool_manager=_BigResultTools(1, 100),
    )

    round_one, round_two, retry = llm.calls
    assert round_one.tokens <= llm.window < round_two.tokens, "the first round fitted; the result is what overflowed"
    assert not retry.tools and retry.tokens < llm.window
    assert reduction is not None and reduction.as_payload() == {"pruned": 0, "folded_chunks": 0, "truncated_parts": 0}


async def test_a_head_an_earlier_tool_round_was_accepted_with_is_retried_whole_even_when_our_count_has_it_over_the_target() -> None:
    """The head is 90k tokens by our count: over the 76k target. Round one (the head and the schemas) was ACCEPTED, round
    two (with a 50k-token tool result in it) was not. The provider has shown the head fits, so the text-only retry sends it
    whole; cutting it to the target would shed 14k tokens of a head that was never the problem."""
    head = [Message(role="user" if i % 2 == 0 else "assistant", parts=[TextPart(text="x" * 60_000)]) for i in range(6)]
    llm = _Window(
        window=CONTEXT_LENGTH - 4_096,
        script=lambda call: _exec_round("whatever") if call.is_summary and call.tools and len(llm.summaries) == 1 else None,
    )
    _, reduction = await CompactionStrategy()._full_compact(  # noqa: SLF001
        head=head, agent=make_agent(), llm=llm, model=_strategy_model(CONTEXT_LENGTH),  # type: ignore[arg-type]
        tool_manager=_BigResultTools(1, 100),
    )

    round_one, round_two, retry = llm.calls
    assert round_one.tokens <= llm.window < round_two.tokens, "round one was accepted; the tool result overflowed round two"
    assert not retry.tools and retry.tokens < llm.window
    assert reduction is not None and reduction.as_payload() == {"pruned": 0, "folded_chunks": 0, "truncated_parts": 0}
    assert retry.text.count("x" * 60_000) == 6, "all six messages, whole"


async def test_a_small_context_model_recovers_by_leaving_the_one_big_result_out() -> None:
    """A 16k window: one 15k-token tool result is the whole head. Leaving it out is one small call. The recovery used
    to refuse here (no room for a FOLD), which it did on every window under about 18k."""
    head = [
        Message(role="user", parts=[TextPart(text="please read the log")]),
        Message(role="assistant", parts=[ToolCallPart(id="c1", name="cat", arguments={"p": "log"})]),
        Message(role="tool", parts=[ToolResultPart(id="c1", output="L" * 60_000)]),
        Message(role="assistant", parts=[TextPart(text="the log shows X")]),
    ]
    llm = _Window(window=16_384 - 4_096)                                 # the input the call can hold beside its summary
    message, reduction = await _compact(head, llm, 16_384)

    assert reduction is not None and (reduction.pruned, reduction.folded_chunks) == (1, 0)
    assert len(llm.calls) == 2 and llm.calls[1].tokens < 1_000 and "SUMMARY" in message.parts[0].text


async def test_a_window_that_leaves_no_room_for_any_input_fails_naming_the_summariser() -> None:
    head = [Message(role="user", parts=[TextPart(text="x" * 40_000)]), Message(role="assistant", parts=[TextPart(text="ok")])]
    llm = _Window(window=1)                                              # rejects whatever it is sent
    with pytest.raises(SummariserOverflow, match="no room for any input"):
        await _compact(head, llm, 4_200)
    assert len(llm.calls) == 1, "no call that cannot work"


async def test_a_window_too_small_to_fold_in_fails_instead_of_sending_a_fold_call_that_cannot_fit() -> None:
    """On an 8k window a fold call (a chunk plus the summary so far, up to 4k) cannot fit, so the retry may be ONE call.
    A head that needs three is not folded anyway: that would be a call that overflows by construction."""
    head = [Message(role="user" if i % 2 == 0 else "assistant", parts=[TextPart(text="x" * 8_000)]) for i in range(3)]
    llm = _Window(window=8_192 - 4_096)
    with pytest.raises(SummariserOverflow, match="bounded at 1"):
        await _compact(head, llm, 8_192)
    assert len(llm.calls) == 1, "only the rejected call was made"


# ----------------------------------------- a turn that overflowed after it ran a tool, then its summariser did


def _overflows_after_one_tool_round(counter: str = "echo ran >> counter.txt"):
    """A turn that runs one tool, overflows on its next call, and answers once it has been compacted and replayed."""
    seen: list[int] = []

    def script(call: _Call):
        if call.is_summary:
            return None
        seen.append(call.index)
        if len(seen) == 1:
            return _exec_round(counter)
        return BadRequestError(OVERFLOW) if len(seen) == 2 else None

    return script


@NEEDS_EXEC
async def test_a_summariser_that_overflows_after_the_turn_ran_a_tool_is_recovered_and_the_tool_runs_once(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_replies(workspace, session)
        llm = _Window(window=10**9, reject_summaries=1, script=_overflows_after_one_tool_round())
        events = await _turn(session, llm)

        assert (workspace.root / "counter.txt").read_text().splitlines() == ["ran"], "the replay does not run the tool again"
        rejected, *retries = llm.summaries
        assert retries and all(not c.tools for c in retries), "the summariser's retry is text only"
        (marker,) = _markers(workspace, session)
        assert marker["payload"]["summary_input_reduced"]["folded_chunks"] == (len(retries) if len(retries) > 1 else 0)
        assert "done" in "".join(e.text for e in events if isinstance(e, TextDelta)), "the turn went on and answered"
    await _with_session(tmp_path, body)


@NEEDS_EXEC
async def test_a_summariser_that_overflows_twice_ends_the_turn_naming_it_and_keeps_the_round_the_turn_ran(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_replies(workspace, session)
        llm = _Window(window=10**9, reject_summaries=2, script=_overflows_after_one_tool_round())
        with pytest.raises(SummariserOverflow) as failed:
            await _turn(session, llm)

        assert len(llm.summaries) == 2 and len(llm.turns) == 2, "the turn's two calls, the summariser's call and its one retry"
        assert (workspace.root / "counter.txt").read_text().splitlines() == ["ran"]
        error = failed.value
        assert error.ended_detail_code == "summariser_overflow"
        assert error.forced_compaction is True and error.replay_attempted is False
        assert error.persisted_rounds == 1, "the round the turn ran is in the history, written once, by the chokepoint"
        messages = [line for line in _lines(workspace, session) if "role" in line]
        calls = [p["id"] for m in messages for p in m["parts"] if p.get("type") == "tool_call"]
        results = [p["id"] for m in messages if m["role"] == "tool" for p in m["parts"]]
        assert len(calls) == 1 and calls == results, "one call, one result, no duplicate id"
        assert _markers(workspace, session) == []
    await _with_session(tmp_path, body)


# --------------------------------------------------------------- a tool-enabled summariser


@NEEDS_EXEC
async def test_a_tool_enabled_summariser_that_overflows_in_round_one_runs_no_tool_and_retries_text_only(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_rounds(workspace, session, turns=3, rounds=4)
        # a model that calls the tool whenever it is offered one: if the retry offered them, one would run
        llm = _Window(script=lambda call: _exec_round("echo ran >> counter.txt", text="S") if call.is_summary and call.tools else None)
        await _turn(session, llm, tool_access=True)

        first, retry = llm.summaries
        assert first.tools, "the first call offered the tools"
        assert not retry.tools, "the retry does not"
        assert not (workspace.root / "counter.txt").exists(), "no tool ran"
        (marker,) = _markers(workspace, session)
        assert marker["payload"]["summary_input_reduced"]["pruned"] > 0
    await _with_session(tmp_path, body)


@NEEDS_EXEC
async def test_a_tool_loop_that_overflows_in_a_later_round_ends_with_the_summary_it_has_and_runs_no_tool_twice(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_rounds(workspace, session, turns=3, rounds=4)
        rounds: list[int] = []

        def script(call: _Call):
            if not call.is_summary:
                return None
            rounds.append(call.index)
            if len(rounds) == 1:
                return _exec_round("echo ran >> counter.txt", text="THE SUMMARY, WRITTEN IN ROUND ONE")
            return BadRequestError(OVERFLOW)                                          # round two: the results outgrew the window

        llm = _Window(window=10**9, script=script)
        await _turn(session, llm, tool_access=True)

        assert [c.is_summary for c in llm.calls] == [True, True, False], "round one, round two (rejected), the turn"
        assert (workspace.root / "counter.txt").read_text().splitlines() == ["ran"], "the tool ran once, not again"
        (marker,) = _markers(workspace, session)
        assert marker["payload"]["summary"].endswith("THE SUMMARY, WRITTEN IN ROUND ONE")
        assert marker["payload"]["summary_input_reduced"] == {
            "pruned": 0, "folded_chunks": 0, "truncated_parts": 0, "tool_loop_cut_round": 2,
        }, "the head was never reduced, but the loop was cut in round two and the marker says so"
    await _with_session(tmp_path, body)


@NEEDS_EXEC
async def test_a_tool_loop_that_fails_for_another_reason_in_a_later_round_is_not_swallowed(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_rounds(workspace, session, turns=3, rounds=4)
        seen: list[int] = []

        def script(call: _Call):
            if not call.is_summary:
                return None
            seen.append(call.index)
            return _exec_round("echo ran >> counter.txt", text="A SUMMARY") if len(seen) == 1 else RateLimitError("slow down")

        llm = _Window(window=10**9, script=script)
        with pytest.raises(RateLimitError):
            await _turn(session, llm, tool_access=True)
        assert len(llm.summaries) == 2 and _markers(workspace, session) == [], "a summary is not made of a failed loop"
    await _with_session(tmp_path, body)


@NEEDS_EXEC
async def test_a_tool_loop_that_overflows_in_round_two_with_no_summary_yet_retries_text_only_without_a_tool(tmp_path) -> None:
    async def body(workspace, session):
        await _seed_rounds(workspace, session, turns=3, rounds=4)
        seen: list[int] = []

        def script(call: _Call):
            if not call.is_summary:
                return None
            seen.append(call.index)
            if len(seen) == 1:
                return _exec_round("echo ran >> counter.txt")                         # tools only: no text yet
            if len(seen) == 2:
                return BadRequestError(OVERFLOW)
            return None                                                                # the text-only retry answers

        llm = _Window(window=10**9, script=script)
        await _turn(session, llm, tool_access=True)

        assert [bool(c.tools) for c in llm.summaries] == [True, True, False]
        assert (workspace.root / "counter.txt").read_text().splitlines() == ["ran"], "the tool still ran exactly once"
        (marker,) = _markers(workspace, session)
        assert "summary_input_reduced" in marker["payload"]
    await _with_session(tmp_path, body)
