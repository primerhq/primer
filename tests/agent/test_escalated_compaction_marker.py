"""R9 on a real executor and workspace: an ESCALATED compaction writes ONE marker, and the next turn's marker folds it.

When the first summary plus the kept tail is still over the trigger, ``_tier2`` summarises the head again with only
the protected part kept: one bounded escalation, in memory, text only. The strategy-level tests
(``test_tier2_keeps_the_turn.py``) pin the two summariser calls, the text-only escalation and the bound. What they
cannot pin is what reaches the disk and what the NEXT turn builds on it, which is where a chained marker can lose
the question or keep a summary that was never the one returned:

* exactly ONE ``compaction_marker`` line is written for the escalated compaction, carrying the ESCALATION's summary
  (not the first pass's) and the floor tail (the question the turn answers);
* the reader returns ``[summary, question, reply]`` for it, and the first pass's summary text is nowhere in the file;
* the next turn's compaction folds that summary into its own: the reader returns ``[summary 2, ...]`` with summary 1
  gone, the new question still the unanswered input, and the second summariser call was sent summary 1;
* the escalation re-reads the WHOLE head, which can be more than the first pass read and over the window: its own
  call is then recovered like any summariser call (text only, folded) and the compaction still writes ONE marker.
"""

from __future__ import annotations

import json

from primer.agent.compaction import CompactionStrategy
from primer.model.chat import Done, Message, TextDelta, TextPart
from primer.model.except_ import BadRequestError
from primer.workspace.session import reconstruct_compacted_history
from tests._support.off_golden import append_messages, assistant_message, open_session, run_turn, user_message

QUESTION = "now do the thing"
FIRST_PASS = "FIRST-PASS-SUMMARY " + "s" * 80_000        # about 20k tokens: the model ignored the allowance
ESCALATED = "ESCALATED-SUMMARY, written from what was left after the first pass was too big"


def _strategy() -> CompactionStrategy:
    # a tail that keeps most turns, so the first pass leaves summary + tail over the trigger and the floor is smaller
    return CompactionStrategy(tail_turns=8, tail_budget_fraction=1.0)


async def _seed(workspace, session, *, units: int, tag: str) -> None:
    """``units`` user/assistant pairs, each user message 10k tokens of text (nothing a prune can give back)."""
    for i in range(units):
        await append_messages(
            workspace, session, user_message(f"{tag}{i}: " + "x" * 40_000), assistant_message(f"{tag} reply {i}"),
        )
    await append_messages(workspace, session, user_message(QUESTION))


def _lines(workspace, session) -> list[str]:
    path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _markers(lines: list[str]) -> list[dict]:
    return [rec for rec in map(json.loads, lines) if rec.get("kind") == "compaction_marker"]


def _text(message: Message) -> str:
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


class _Replies:
    """Answers each call with the next scripted text and keeps the messages it was sent (the summariser's prompt is
    what the second test reads)."""

    def __init__(self, *texts: str, window: int | None = None) -> None:
        self._texts = list(texts)
        self.sent: list[list[Message]] = []
        self.rejected = 0
        self.window = window          # a call over it is rejected as a context overflow, as a provider does
        self.session_id = ""

    async def list_models(self) -> list[str]:
        return ["m"]

    def unused(self) -> int:
        return len(self._texts)

    def stream(self, *, model, messages, **kwargs):
        self.sent.append(list(messages))
        if self.window is not None and CompactionStrategy._estimate_tokens(messages) > self.window:  # noqa: SLF001
            self.rejected += 1
            return self._reject()
        return self._run(self._texts.pop(0))

    async def _reject(self):
        raise BadRequestError("This model's maximum context length is 100000 tokens, however you requested more")
        yield  # pragma: no cover -- makes this an async generator, like the adapters: the error surfaces on iteration

    async def _run(self, text: str):
        yield TextDelta(text=text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


async def test_an_escalated_compaction_writes_one_marker_with_the_escalations_summary_and_the_floor_tail(tmp_path) -> None:
    backend, workspace, session = await open_session(tmp_path)
    try:
        await _seed(workspace, session, units=12, tag="a")
        # the summariser (too large), the escalation (the head summarised again, floor tail), the turn
        llm = _Replies(FIRST_PASS, ESCALATED, "done")
        await run_turn(session, llm, compaction=_strategy())

        lines = _lines(workspace, session)
        (marker,) = _markers(lines)
        payload = marker["payload"]
        assert ESCALATED in payload["summary"] and "FIRST-PASS-SUMMARY" not in payload["summary"]
        assert payload["outcome"] == "summarised" and payload["unreducible"] is None
        assert payload["tokens_after"] < payload["trigger_tokens"], "the escalation is what made it fit"
        kept = [Message.model_validate(m) for m in payload["kept_tail_messages"]]
        assert [_text(m) for m in kept] == [QUESTION], "the floor: the question the turn answers, nothing else"
        assert "FIRST-PASS-SUMMARY" not in "\n".join(lines), "the discarded first pass reached the disk nowhere"

        history = reconstruct_compacted_history(lines)
        assert [m.role for m in history] == ["assistant", "user", "assistant"]
        assert ESCALATED in _text(history[0]) and _text(history[1]) == QUESTION and _text(history[2]) == "done"
        assert len(llm.sent) == 3
    finally:
        await session.aclose()
        await backend.aclose()


async def test_the_next_turns_compaction_folds_an_escalated_marker_and_keeps_its_question(tmp_path) -> None:
    backend, workspace, session = await open_session(tmp_path)
    try:
        await _seed(workspace, session, units=12, tag="a")
        first = _Replies(FIRST_PASS, ESCALATED, "done")
        await run_turn(session, first, compaction=_strategy())

        # turn 2: the session grows past the trigger again on top of the escalated marker
        await _seed(workspace, session, units=12, tag="b")
        second = _Replies("SUMMARY-TWO", "done again")
        await run_turn(session, second, compaction=_strategy())

        lines = _lines(workspace, session)
        first_marker, second_marker = _markers(lines)
        assert ESCALATED in first_marker["payload"]["summary"]
        assert "SUMMARY-TWO" in second_marker["payload"]["summary"]
        assert second_marker["seq"] > first_marker["seq"]

        history = reconstruct_compacted_history(lines)
        texts = [_text(m) for m in history]
        assert "SUMMARY-TWO" in texts[0] and history[0].role == "assistant", "the newest marker's summary leads"
        assert not any(ESCALATED in t for t in texts), "summary 1 is folded into summary 2: it is not history any more"
        assert texts.count(QUESTION) == 1, "the question is kept once (the second turn's), not once per marker"
        assert texts[-2:] == [QUESTION, "done again"], "the second turn's question is the last thing before its reply"
        # summary 2 is a summary of summary 1 and what came after it: the summariser was sent the escalated text
        assert len(second.sent) == 2
        assert any(ESCALATED in _text(m) for m in second.sent[0]), "the second summariser read summary 1"
    finally:
        await session.aclose()
        await backend.aclose()


async def test_an_escalation_whose_own_call_overflows_is_recovered_and_still_one_marker(tmp_path) -> None:
    """The escalation summarises the WHOLE head again (only the protected part is kept), which is more than the first
    pass read: here 12 units of 10k tokens against a 100k window. Its call is rejected, recovered once (text only, on a
    folded input), and the compaction still writes ONE marker, carrying the escalation's summary and how it was read."""
    backend, workspace, session = await open_session(tmp_path)
    try:
        await _seed(workspace, session, units=12, tag="a")
        # the first pass fits (4 units), the escalation's call does not (rejected, no reply consumed), then its fold
        llm = _Replies(FIRST_PASS, "FOLD-1", "FOLD-2", "FOLD-3", "done", window=100_000)
        await run_turn(session, llm, compaction=_strategy())

        lines = _lines(workspace, session)
        (marker,) = _markers(lines)
        payload = marker["payload"]
        assert llm.rejected == 1, "the escalation's own first call was the one rejected"
        assert "FOLD-3" in payload["summary"] and "FIRST-PASS-SUMMARY" not in "\n".join(lines)
        assert payload["summary_input_reduced"] == {"pruned": 0, "folded_chunks": 3, "truncated_parts": 0}
        kept = [Message.model_validate(m) for m in payload["kept_tail_messages"]]
        assert [_text(m) for m in kept] == [QUESTION], "the floor tail, as for any escalated compaction"
        assert payload["outcome"] == "summarised" and payload["tokens_after"] < payload["trigger_tokens"]
    finally:
        await session.aclose()
        await backend.aclose()

