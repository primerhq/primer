"""A deterministic scripted session that pins today's compaction behaviour ("mode off") byte for byte.

The prompt-budget work replaces the character-heuristic trigger with measured token counts, mode by
mode, and its kill switch (``off``) must stay identical to the code that existed before any of it.
This module runs ONE scripted session on the real ``WorkspaceAgentExecutor`` over a real local
workspace session, crossing every path the budget work touches, and records what a refactor could
change without failing a unit test:

* turn 1, TIER 1: four tool results over the per-output threshold and over the total threshold push
  the history past the trigger and the strategy prunes them (prune-only, no summary);
* turn 2, TIER 2: user turns that pruning cannot shrink force a full compaction (a summary and a
  marker);
* turn 3, OVERFLOW REPLAY: the model rejects the turn with a context-overflow error, the executor
  force-compacts and re-runs the loop. The session was compacted before (a marker and its kept tail
  are seeded), so this compaction runs over a reconstructed ``[summary, *tail, ...]`` and the new
  summary folds the old one: the chain every long session lives in;
* turn 4, DEFERRED STEER: two steers arrive while the compaction summary call is in flight, are held
  back and land after the marker, in submission order;
* turns 5 and 6, THE BOUNDARY: two small sessions whose history estimate PLUS the fixed overhead (system
  prompt and tool schemas, which count against the trigger) is exactly one token under the trigger and
  exactly at it, so the end-to-end golden pins whether the trigger fires at ``<`` or ``<=``
  (tier 1 is in memory, so the difference shows in the prompt: raw tool results, then placeholders).

For every turn it records the prompt of every LLM call, the markers, and the persisted
``messages.jsonl`` line by line and as a whole (timestamps and the session id are normalised: they
are the only bytes that differ between runs). ``tests/agent/test_off_mode_golden.py`` compares a
fresh run with the committed fixture; ``scripts/capture_off_golden.py`` regenerates the fixture and
must be run from a clean checkout of the commit whose behaviour is being pinned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from primer.agent.compaction import CompactionStrategy
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done, Message, StreamEvent, TextDelta, TextPart, ToolCallPart, ToolResultPart,
)
from primer.model.except_ import BadRequestError
from primer.model.model_profile import ModelProfileConfig
from primer.model.workspace import (
    LocalWorkspaceConfig, WorkspaceProvider, WorkspaceProviderType, WorkspaceTemplate,
)
from primer.model.workspace_session import AgentBinding, SessionMessageKind, SessionMessageRecord
from primer.model_profile import ResolvedModel
from primer.workspace import WorkspaceBackendFactory

CONTEXT_LENGTH = 100_000
TOOL_RESULT_CHARS = 90_000   # about 22.5k tokens by the heuristic: over the 20k per-output threshold
BIG_USER_CHARS = 120_000     # about 30k tokens: user text, which tier 1 cannot prune


# ---------------------------------------------------------------- scripted LLM

@dataclass
class Events:
    events: list[StreamEvent]


@dataclass
class Raise:
    error: Exception


@dataclass
class Gate:
    """Signal ``entered``, wait for ``release``, then stream ``events``."""

    entered: asyncio.Event
    release: asyncio.Event
    events: list[StreamEvent]
    error: Exception | None = None  # raised after the release instead of streaming ``events``


def text_events(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text, index=0), Done(stop_reason="stop", raw_reason="stop")]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def part_fingerprint(part: Any, session_id: str) -> list[Any]:
    # Normalised like the persisted file: the system prompt names the session and the compaction
    # summary carries a timestamp, the only two things that differ between runs.
    body = normalise(json.dumps(part.model_dump(mode="json"), sort_keys=True), session_id)
    return [getattr(part, "type", type(part).__name__), len(body), _digest(body)]


def tool_fingerprint(tool: Any, session_id: str) -> list[Any]:
    body = normalise(json.dumps(tool.model_dump(mode="json"), sort_keys=True), session_id)
    return [tool.id, len(body), _digest(body)]


def message_fingerprint(message: Message, session_id: str) -> dict[str, Any]:
    return {"role": message.role, "parts": [part_fingerprint(p, session_id) for p in message.parts]}


class ScriptedLLM:
    """Consumes scripted steps in call order and fingerprints every prompt it is handed.

    A step is ``Events``, ``Raise`` or ``Gate``. Calling it with no step left is a failure, and so
    is leaving steps unused at the end of a turn: either means the session ran a different number of
    LLM calls than the scenario expects, which is exactly what the golden must catch.
    """

    def __init__(self) -> None:
        self._steps: list[Events | Raise | Gate] = []
        self.calls: list[dict[str, Any]] = []
        self.session_id = ""  # set once the session exists, so fingerprints can drop it

    def extend(self, steps: list[Events | Raise | Gate]) -> None:
        self._steps.extend(steps)

    def unused(self) -> int:
        return len(self._steps)

    async def list_models(self) -> list[str]:
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        tools = kwargs.get("tools") or []
        response_format = kwargs.get("response_format")
        self.calls.append({
            "call": len(self.calls) + 1,
            "model": model,
            "messages": [message_fingerprint(m, self.session_id) for m in messages],
            "tool_ids": sorted(t.id for t in tools),
            # each tool as [id, length, digest] of its whole normalised dump, so a changed description or
            # argument schema is a change, not just a changed id
            "tools": sorted(tool_fingerprint(t, self.session_id) for t in tools),
            # the call's other keyword arguments BY VALUE (temperature, max_output_tokens, tool_choice...);
            # response_format can be a model class or a schema, so it is digested
            "kwargs": {k: repr(v) for k, v in sorted(kwargs.items()) if k not in ("tools", "response_format")},
            "response_format": None if response_format is None else _digest(repr(response_format)),
        })
        if not self._steps:
            raise AssertionError(f"the session made LLM call {len(self.calls)} but the scenario scripted no step for it")
        return self._run(self._steps.pop(0))

    async def _run(self, step: Events | Raise | Gate):
        if isinstance(step, Raise):
            raise step.error
        if isinstance(step, Gate):
            step.entered.set()
            await step.release.wait()
            if step.error is not None:
                raise step.error
        for event in step.events:
            yield event


# ------------------------------------------------------------------- the session

def make_agent() -> Agent:
    return Agent(
        id="golden", description="golden", model=AgentModel(profile_id="p--m"),
        system_prompt=["Be terse."],
    )


def make_model() -> ResolvedModel:
    return ResolvedModel(
        profile_id="golden-profile", provider_id="golden-provider", model_name="golden-model",
        context_length=CONTEXT_LENGTH, config=ModelProfileConfig(),
    )


async def open_session(root: Path):
    backend = WorkspaceBackendFactory.create(WorkspaceProvider(
        id="local-1", provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path=str(root / "wsroot")),
    ))
    await backend.initialize()
    workspace = await backend.create(WorkspaceTemplate(
        id="golden", description="golden", provider_id="local-1", files=[],
    ))
    session = await workspace.start_session(
        AgentBinding(agent_id="golden", agent_name="Golden", registered_tool_ids=[]),
        instructions="hello",
    )
    return backend, workspace, session


async def append_messages(workspace, session, *messages: Message) -> None:
    """Seed history: append whole Message lines, as the executor itself would persist them.

    ``append_message_line`` takes the session's messages lock itself, so it is NOT wrapped in one
    here (the lock is not reentrant: wrapping it deadlocks).
    """
    for message in messages:
        await workspace.append_message_line(session.session_id, (message.model_dump_json() + "\n").encode())


PRIOR_SUMMARY = "[earlier conversation compacted on 2026-10-05T00:00:00+00:00]\n\nSUMMARY-0: what this session did before turn 3"


async def append_prior_marker(workspace, session, *, summary: str, kept_tail: list[Message]) -> None:
    """Seed a compaction that already happened: a ``compaction_marker`` and the tail it kept, written as the
    executor writes one (see ``WorkspaceAgentExecutor._replace_compacted_head``). Everything physically before
    it is folded by the reader, except the kept tail, so the history after it reads ``[summary, *kept_tail]``."""
    path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
    boundary = max(
        (obj["seq"] for obj in map(json.loads, path.read_text(encoding="utf-8").splitlines())
         if isinstance(obj, dict) and isinstance(obj.get("seq"), int)),
        default=0,
    )
    created = datetime(2026, 10, 5, tzinfo=timezone.utc)
    marker = SessionMessageRecord(
        seq=boundary + 1,
        kind=SessionMessageKind.COMPACTION_MARKER,
        payload={
            "summary": summary,
            "kept_tail_messages": [json.loads(m.model_dump_json()) for m in kept_tail],
            "replaced_from_seq": 1,
            "replaced_to_seq": boundary,
            "model": "golden-model",
            "tokens_before": 90_000,
            "tokens_after": 20_000,
            "outcome": "summarised",
            "unreducible": None,
            "trigger_tokens": trigger_tokens(),
            "fixed_overhead_tokens": await fixed_overhead(session),
            "created_at": created.isoformat(),
        },
        created_at=created,
    )
    await workspace.append_message_line(session.session_id, (marker.model_dump_json() + "\n").encode())


def tool_round(i: int) -> list[Message]:
    call = ToolCallPart(id=f"call_{i}", name="exec", arguments={"cmd": f"cat part_{i}"})
    return [
        Message(role="assistant", parts=[call]),
        Message(role="tool", parts=[ToolResultPart(id=f"call_{i}", output=chr(ord("a") + i) * TOOL_RESULT_CHARS)]),
    ]


def user_message(text: str) -> Message:
    return Message(role="user", parts=[TextPart(text=text)])


def assistant_message(text: str) -> Message:
    return Message(role="assistant", parts=[TextPart(text=text)])


def make_executor(
    session, llm, *, llm_model=None, compaction=None, wrap_tools=None,
) -> WorkspaceAgentExecutor:
    """The real executor over a real session. ``wrap_tools`` receives the real tool manager and returns the one to use."""
    manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    if wrap_tools is not None:
        manager = wrap_tools(manager)
    return WorkspaceAgentExecutor(
        agent=make_agent(), llm=llm,  # type: ignore[arg-type]
        llm_model=llm_model or make_model(), tool_manager=manager, session=session,
        compaction=compaction or CompactionStrategy(),
    )


async def fixed_overhead(session) -> int:
    """The estimated size of the part of every prompt no history can give back, as the executor counts it."""
    return await make_executor(session, ScriptedLLM()).fixed_overhead_tokens()


async def run_turn(
    session, llm: ScriptedLLM, *, collect: list | None = None, llm_model=None, compaction=None, wrap_tools=None,
) -> None:
    """One turn on the real executor; ``collect`` receives every event the turn yields."""
    executor = make_executor(session, llm, llm_model=llm_model, compaction=compaction, wrap_tools=wrap_tools)
    async for event in executor.invoke([]):
        if collect is not None:
            collect.append(event)
    assert llm.unused() == 0, f"{llm.unused()} scripted LLM step(s) were never used this turn"


# ------------------------------------------------------------- what is recorded

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


def normalise(text: str, session_id: str) -> str:
    return _TIMESTAMP.sub("<ts>", text).replace(session_id, "<sid>")


def _preview(obj: dict[str, Any]) -> str:
    """A short human-readable label for a persisted Message line, so a diff names what moved."""
    for part in obj.get("parts", []):
        if part.get("type") == "text":
            return part["text"][:40]
        if part.get("type") == "tool_result":
            return f"<tool_result {part.get('id')} {len(part.get('output', ''))} chars>"
        if part.get("type") == "tool_call":
            return f"<tool_call {part.get('id')}>"
    return ""


def capture_file(text: str, session_id: str) -> dict[str, Any]:
    normalised = normalise(text, session_id)
    lines: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    for line in normalised.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and "kind" in obj and isinstance(obj.get("seq"), int):
            lines.append({"record": obj["kind"], "seq": obj["seq"], "len": len(line), "sha": _digest(line)})
            if obj["kind"] == "compaction_marker":
                payload = dict(obj["payload"])
                summary = payload.pop("summary", "")
                kept = payload.pop("kept_tail_messages", None)
                marker = {"seq": obj["seq"], **payload, "summary_len": len(summary), "summary_sha": _digest(summary)}
                if kept is not None:
                    # the tail the marker keeps verbatim, by label, size and digest (the texts are 120k chars)
                    marker["kept_tail"] = [
                        {"message": m.get("role"), "preview": _preview(m), "len": len(json.dumps(m)), "sha": _digest(json.dumps(m, sort_keys=True))}
                        for m in kept
                    ]
                markers.append(marker)
        else:
            lines.append({"message": obj.get("role"), "preview": _preview(obj), "len": len(line), "sha": _digest(line)})
    return {
        "bytes": len(normalised.encode("utf-8")),
        "sha256": hashlib.sha256(normalised.encode("utf-8")).hexdigest(),
        "lines": lines,
        "markers": markers,
    }


# ---------------------------------------------------------------- the boundary turns

async def _history_estimate(session) -> int:
    """The heuristic's size of the history the executor would hand ``maybe_compact`` right now."""
    executor = make_executor(session, ScriptedLLM())
    return CompactionStrategy._estimate_tokens(await executor._read_messages_jsonl())


def trigger_tokens() -> int:
    strategy = CompactionStrategy()
    return int(strategy.trigger_ratio * strategy._effective_budget(make_model()))


async def boundary_turn(root: Path, llm: ScriptedLLM, name: str, delta: int) -> dict[str, Any]:
    """One turn on a fresh session whose history estimate plus the fixed overhead is EXACTLY ``trigger + delta`` tokens.

    Three tool results over the per-output and total prune thresholds give tier 1 something to do; one
    trailing user message of computed length pads the estimate to the target. Below the trigger nothing
    happens and the prompt carries the raw results; at it, tier 1 prunes them in memory.
    """
    backend, workspace, session = await open_session(root)
    try:
        llm.session_id = session.session_id
        for i in range(3):
            await append_messages(workspace, session, *tool_round(i))
        target = trigger_tokens() + delta
        fixed = await fixed_overhead(session)  # counted against the trigger too: target = history + fixed
        padding_tokens = target - fixed - await _history_estimate(session) - 8  # 8 = the pad message's own overhead
        assert padding_tokens >= 1, "the seeded history is already past the target"
        await append_messages(workspace, session, user_message("p" * (4 * padding_tokens)))
        assert await _history_estimate(session) + fixed == target, "the padding must land the estimate exactly on the target"
        llm.extend([Events(text_events("boundary-ok"))])
        before = len(llm.calls)
        await run_turn(session, llm)
        path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
        return {
            "turn": name,
            "target_estimate": target,
            "llm_calls": len(llm.calls) - before,
            "file": capture_file(path.read_text(encoding="utf-8"), session.session_id),
        }
    finally:
        await session.aclose()
        await backend.aclose()


# ---------------------------------------------------------------- the scenario

async def recorded_turn(
    root: Path, llm: ScriptedLLM, name: str, seed, steps: list[Events | Raise | Gate], *, during=None,
) -> dict[str, Any]:
    """One turn on ITS OWN fresh session: seed it, script the model, run the turn, record what happened.

    Every turn owns its session so that a turn's record depends only on its own seed and on the code, never on
    what an earlier turn left behind: a change to one turn's behaviour cannot cascade into the next, which is
    what lets a re-capture declare exactly the turns that may change (``scripts/capture_off_golden.py``).
    ``during`` replaces the plain run for a turn that needs something to happen while it is in flight.
    """
    backend, workspace, session = await open_session(root)
    try:
        llm.session_id = session.session_id
        await seed(workspace, session)
        llm.extend(steps)
        before = len(llm.calls)
        if during is None:
            await run_turn(session, llm)
        else:
            await during(workspace, session)
        path = workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
        return {
            "turn": name,
            "llm_calls": len(llm.calls) - before,
            "file": capture_file(path.read_text(encoding="utf-8"), session.session_id),
        }
    finally:
        await session.aclose()
        await backend.aclose()


async def run_scenario() -> dict[str, Any]:
    """Run the six turns, each on its own session, and return everything the golden pins."""
    with tempfile.TemporaryDirectory(prefix="primer-off-golden-") as tmp:
        root = Path(tmp)
        llm = ScriptedLLM()
        turns: list[dict[str, Any]] = []

        # Turn 1, tier 1: 4 x ~22.5k-token tool results (total ~90k, over the 40k total and the 82.6k trigger).
        async def seed_1(workspace, session) -> None:
            for i in range(4):
                await append_messages(workspace, session, *tool_round(i))
            await append_messages(workspace, session, user_message("turn 1: go on"))

        turns.append(await recorded_turn(root, llm, "1-tier1-prune", seed_1, [Events(text_events("turn-1-ok"))]))

        # Turn 2, tier 2: user text pruning cannot shrink. The history turn 1 leaves (its tool rounds and its
        # reply), then four big exchanges the model answered, then the input this turn answers: input the model
        # has not answered is never summarised, so a history of nothing but unanswered user text is not
        # compactable.
        async def seed_2(workspace, session) -> None:
            await seed_1(workspace, session)
            await append_messages(workspace, session, assistant_message("turn-1-ok"))
            for i in range(4):
                await append_messages(
                    workspace, session, user_message(chr(ord("A") + i) * BIG_USER_CHARS), assistant_message(f"turn 2 reply {i}"),
                )
            await append_messages(workspace, session, user_message("turn 2: next"))

        turns.append(await recorded_turn(
            root, llm, "2-tier2-summary", seed_2, [Events(text_events("SUMMARY-2")), Events(text_events("turn-2-ok"))],
        ))

        # Turn 3, overflow replay: the turn's own call is rejected as a context overflow and the forced
        # compaction replays it. The session was compacted before: a marker and the exchange it kept, so the
        # history is [summary, question, answer, ...] and this compaction summarises (and folds) that summary
        # too. Then seven answered exchanges and this turn's input (the pending suffix, kept whatever happens),
        # so the forced compaction has a head to summarise.
        async def seed_3(workspace, session) -> None:
            await append_prior_marker(
                workspace, session, summary=PRIOR_SUMMARY,
                kept_tail=[user_message("turn 3 earlier question"), assistant_message("turn 3 earlier answer")],
            )
            for i in range(7):
                await append_messages(
                    workspace, session, user_message(f"turn 3 filler {i}: " + ("f" * 2000)), assistant_message(f"turn 3 reply {i}"),
                )
            await append_messages(workspace, session, user_message("turn 3: next"))

        overflow = BadRequestError("This model's maximum context length is 100000 tokens, however you requested more")
        turns.append(await recorded_turn(
            root, llm, "3-overflow-replay", seed_3,
            [Raise(overflow), Events(text_events("SUMMARY-3")), Events(text_events("turn-3-ok"))],
        ))

        # Turn 4, deferred steers: two steers land while the summary call is in flight. Four answered big
        # exchanges end the history (no pending input), so the tail shrinks to its budget and a head is
        # summarised; a history with nothing before the unanswered input is reported unreducible instead (see
        # the tier-2 tests).
        async def seed_4(workspace, session) -> None:
            for i in range(4):
                await append_messages(
                    workspace, session, user_message(chr(ord("W") + i) * BIG_USER_CHARS), assistant_message(f"turn 4 reply {i}"),
                )

        entered, release = asyncio.Event(), asyncio.Event()

        async def during_4(workspace, session) -> None:
            task = asyncio.create_task(run_turn(session, llm))
            await asyncio.wait_for(entered.wait(), timeout=30)
            await session.append_instruction("STEER-DURING-COMPACTION-1")
            await session.append_instruction("STEER-DURING-COMPACTION-2")
            release.set()
            await asyncio.wait_for(task, timeout=30)

        turns.append(await recorded_turn(
            root, llm, "4-deferred-steers", seed_4,
            [Gate(entered, release, text_events("SUMMARY-4")), Events(text_events("turn-4-ok"))], during=during_4,
        ))

        # Turns 5 and 6: the boundary, each on its own session.
        turns.append(await boundary_turn(root, llm, "5-one-under-the-trigger", -1))
        turns.append(await boundary_turn(root, llm, "6-exactly-at-the-trigger", 0))
        return {"call_count": len(llm.calls), "calls": llm.calls, "turns": turns}
