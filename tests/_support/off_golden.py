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
  force-compacts and re-runs the loop;
* turn 4, DEFERRED STEER: two steers arrive while the compaction summary call is in flight, are held
  back and land after the marker, in submission order;
* turns 5 and 6, THE BOUNDARY: two small sessions whose history estimate is exactly one token under the
  trigger and exactly at it, so the end-to-end golden pins whether the trigger fires at ``<`` or ``<=``
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
from primer.model.workspace_session import AgentBinding
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


async def run_turn(session, llm: ScriptedLLM, *, wrap_tools=None, configure=None, messages=None) -> None:
    """One turn on the real executor. ``wrap_tools`` receives the real tool manager and returns the one to use (a
    test that needs a tool to park or to return a huge result wraps it); ``configure`` receives the executor before
    it runs (``WorkspaceAgentExecutor`` rebuilds the agent from a few fields, so an agent setting such as
    ``max_tool_turns`` cannot be passed in: a test that needs one sets it on the executor's own agent)."""
    manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    if wrap_tools is not None:
        manager = wrap_tools(manager)
    executor = WorkspaceAgentExecutor(
        agent=make_agent(), llm=llm,  # type: ignore[arg-type]
        llm_model=make_model(), tool_manager=manager, session=session,
        compaction=CompactionStrategy(),
    )
    if configure is not None:
        configure(executor)
    async for _event in executor.invoke(list(messages or [])):
        pass
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
    manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    executor = WorkspaceAgentExecutor(
        agent=make_agent(), llm=ScriptedLLM(),  # type: ignore[arg-type]
        llm_model=make_model(), tool_manager=manager, session=session, compaction=CompactionStrategy(),
    )
    return CompactionStrategy._estimate_tokens(await executor._read_messages_jsonl())


def trigger_tokens() -> int:
    strategy = CompactionStrategy()
    return int(strategy.trigger_ratio * strategy._effective_budget(make_model()))


async def boundary_turn(root: Path, llm: ScriptedLLM, name: str, delta: int) -> dict[str, Any]:
    """One turn on a fresh session whose history estimate is EXACTLY ``trigger + delta`` tokens.

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
        padding_tokens = target - await _history_estimate(session) - 8  # 8 = the pad message's own overhead
        assert padding_tokens >= 1, "the seeded history is already past the target"
        await append_messages(workspace, session, user_message("p" * (4 * padding_tokens)))
        assert await _history_estimate(session) == target, "the padding must land the estimate exactly on the target"
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

async def run_scenario() -> dict[str, Any]:
    """Run the four turns and return everything the golden pins."""
    with tempfile.TemporaryDirectory(prefix="primer-off-golden-") as tmp:
        backend, workspace, session = await open_session(Path(tmp))
        llm = ScriptedLLM()
        llm.session_id = session.session_id
        turns: list[dict[str, Any]] = []
        messages_path = (
            workspace.root / workspace.template.state_path / "sessions" / session.session_id / "messages.jsonl"
        )

        def record(name: str, calls_before: int) -> None:
            turns.append({
                "turn": name,
                "llm_calls": len(llm.calls) - calls_before,
                "file": capture_file(messages_path.read_text(encoding="utf-8"), session.session_id),
            })

        try:
            # Turn 1, tier 1: 4 x ~22.5k-token tool results (total ~90k, over the 40k total and the 82.6k trigger).
            for i in range(4):
                await append_messages(workspace, session, *tool_round(i))
            await append_messages(workspace, session, user_message("turn 1: go on"))
            llm.extend([Events(text_events("turn-1-ok"))])
            before = len(llm.calls)
            await run_turn(session, llm)
            record("1-tier1-prune", before)

            # Turn 2, tier 2: user text pruning cannot shrink. Four big exchanges the model answered, then the
            # input this turn answers: input the model has not answered is never summarised, so a history of nothing
            # but unanswered user text (what this turn used to be) is not compactable.
            for i in range(4):
                await append_messages(
                    workspace, session, user_message(chr(ord("A") + i) * BIG_USER_CHARS), assistant_message(f"turn 2 reply {i}"),
                )
            await append_messages(workspace, session, user_message("turn 2: next"))
            llm.extend([Events(text_events("SUMMARY-2")), Events(text_events("turn-2-ok"))])
            before = len(llm.calls)
            await run_turn(session, llm)
            record("2-tier2-summary", before)

            # Turn 3, overflow replay: the turn's own call is rejected as a context overflow. The tail split
            # treats an ASSISTANT message as a turn boundary and the head is whatever precedes the 4th most
            # recent one; with fewer than 4 (or that one first in the history) it is empty, nothing is
            # summarised and no LLM call is made. This history starts with a user message and carries 7
            # assistant replies, so the head exists.
            for i in range(5):
                await append_messages(workspace, session, user_message(f"turn 3 filler {i}: " + ("f" * 2000)), assistant_message(f"turn 3 reply {i}"))
            await append_messages(workspace, session, user_message("turn 3: next"))
            llm.extend([
                Raise(BadRequestError("This model's maximum context length is 100000 tokens, however you requested more")),
                Events(text_events("SUMMARY-3")),
                Events(text_events("turn-3-ok")),
            ])
            before = len(llm.calls)
            await run_turn(session, llm)
            record("3-overflow-replay", before)

            # Turn 4, deferred steers: two steers land while the summary call is in flight. Again something
            # must precede the 4th most recent assistant message, or the strategy silently does nothing (see
            # the characterisation tests).
            for i in range(4):
                await append_messages(workspace, session, user_message(chr(ord("W") + i) * BIG_USER_CHARS), assistant_message(f"turn 4 reply {i}"))
            entered, release = asyncio.Event(), asyncio.Event()
            llm.extend([Gate(entered, release, text_events("SUMMARY-4")), Events(text_events("turn-4-ok"))])
            before = len(llm.calls)
            task = asyncio.create_task(run_turn(session, llm))
            await asyncio.wait_for(entered.wait(), timeout=30)
            await session.append_instruction("STEER-DURING-COMPACTION-1")
            await session.append_instruction("STEER-DURING-COMPACTION-2")
            release.set()
            await asyncio.wait_for(task, timeout=30)
            record("4-deferred-steers", before)
        finally:
            await session.aclose()
            await backend.aclose()
        # Turns 5 and 6: the boundary, each on its own session (the main one's history is not under our control).
        turns.append(await boundary_turn(Path(tmp), llm, "5-one-under-the-trigger", -1))
        turns.append(await boundary_turn(Path(tmp), llm, "6-exactly-at-the-trigger", 0))
        return {"call_count": len(llm.calls), "calls": llm.calls, "turns": turns}
