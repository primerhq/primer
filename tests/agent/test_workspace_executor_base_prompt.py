"""The session's launch-time base prompt leads the session's system prompt, and the compactor never sees it (Wiki First W4).

``base_prompt`` is written once, at create, and the executor prepends it
ahead of the agent's own prompt and the workspace fragment (U4). Because
it lives in the SYSTEM slot, ``CompactionStrategy`` - which only ever
receives the history and the turn's new messages - cannot touch it; the
compactor's summariser call never carries it, and the NEXT turn's prompt
still starts with it byte-identical (U6).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Done, Message, TextDelta, TextPart
from tests.agent.test_workspace_executor import (
    _FakeLLM,
    _agent,
    _build_session,
    _drain,
    _model,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)

BASE = "ALWAYS: answer the token question with the token TOKEN-42"


def _executor(
    session,
    *,
    system_prompt=None,
    session_base_prompt=None,
    llm=None,
) -> WorkspaceAgentExecutor:
    return WorkspaceAgentExecutor(
        agent=_agent(system_prompt=system_prompt if system_prompt is not None else ["base"]),
        llm=llm if llm is not None else _FakeLLM(scripts=[]),  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=ToolExecutionManager.for_workspace(toolset_providers={}, session=session),
        session=session,
        session_base_prompt=session_base_prompt,
    )


# ===========================================================================
# U4 -- the composite
# ===========================================================================


async def test_the_base_prompt_comes_first_in_the_composite(tmp_path: Path) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        ex = _executor(session, session_base_prompt=BASE)
        assert ex._agent.system_prompt == [BASE, "base", session.system_prompt_fragment]
    finally:
        await session.aclose()
        await backend.aclose()


@pytest.mark.parametrize("text", [None, ""])
async def test_a_blank_or_absent_base_prompt_leaves_the_composite_untouched(
    tmp_path: Path, text,
) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        ex = _executor(session, session_base_prompt=text)
        assert ex._agent.system_prompt == ["base", session.system_prompt_fragment]
    finally:
        await session.aclose()
        await backend.aclose()


async def test_the_registry_agent_is_not_changed(tmp_path: Path) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        agent = _agent(system_prompt=["base"])
        WorkspaceAgentExecutor(
            agent=agent,
            llm=_FakeLLM(scripts=[]),  # type: ignore[arg-type]
            llm_model=_model(),
            tool_manager=ToolExecutionManager.for_workspace(toolset_providers={}, session=session),
            session=session,
            session_base_prompt=BASE,
        )
        assert agent.system_prompt == ["base"]
    finally:
        await session.aclose()
        await backend.aclose()


async def test_the_model_receives_the_base_prompt_first(tmp_path: Path) -> None:
    """Rendered, not just composed: the first system message the model gets
    starts with the base prompt, byte-identical (plain text renders as
    written; the Jinja render is byte-preserving for text without
    metacharacters)."""
    backend, _ws, session = await _build_session(tmp_path)
    try:
        llm = _FakeLLM(scripts=[[TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")]])
        ex = _executor(session, session_base_prompt=BASE, llm=llm)
        await _drain(ex.invoke([Message(role="user", parts=[TextPart(text="hi")])]))
        sys_msg = llm.calls[0]["messages"][0]
        assert sys_msg.role == "system"
        text = sys_msg.parts[0].text
        assert text.startswith(BASE + "\n\nbase\n\n"), (
            f"the base prompt is not the rendered head, ahead of the agent's own prompt: {text[:80]!r}"
        )
        # And ahead of the workspace fragment (which carries the session id).
        assert text.index(session.session_id) > text.index(BASE)
    finally:
        await session.aclose()
        await backend.aclose()


# ===========================================================================
# U6 -- the compactor never sees it, and the next turn keeps it
# ===========================================================================


def _oversized_history() -> list[Message]:
    """Plain-text history comfortably over the trigger (chars/4 token
    estimate; ~160k tokens against a 108k trigger at the default 128k
    context), with NO tool outputs (tier-1 pruning is a no-op) and a small
    newest turn, so the protected tail fits the budget and tier-2 - the LLM
    call - is the path taken."""
    return [
        Message(role="user", parts=[TextPart(text="x" * 400_000)]),
        Message(role="assistant", parts=[TextPart(text="y" * 4_000)]),
        Message(role="user", parts=[TextPart(text="z" * 240_000)]),
        Message(role="assistant", parts=[TextPart(text="w" * 4_000)]),
    ]


async def test_the_compactor_never_sees_the_base_prompt_and_the_next_turn_keeps_it(
    tmp_path: Path,
) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        llm = _FakeLLM(scripts=[[TextDelta(text="summary", index=0), Done(stop_reason="stop", raw_reason="stop")]])
        ex = _executor(session, session_base_prompt=BASE, llm=llm)
        strat = CompactionStrategy()
        result = await strat.maybe_compact(
            agent=ex._agent,
            llm=llm,
            model=_model(),
            history=_oversized_history(),
            new_messages=[Message(role="user", parts=[TextPart(text="next")])],
        )
        assert result is not None, "the history is oversized; compaction must fire"
        assert result.summary_message is not None, "the tier-2 (LLM) path must be the one taken"

        # The compactor's call(s) - the only LLM call(s) this test makes -
        # carry the serialized history and the compaction prompt, and NONE
        # of it may hold the base prompt.
        assert llm.calls, "the tier-2 (LLM) path must be the one taken"
        for call in llm.calls:
            for msg in call["messages"]:
                for part in msg.parts:
                    assert isinstance(part, TextPart)
                    assert BASE not in part.text, "the compactor's input held the base prompt"

        # The next turn rebuilds its prompt from the compacted history; the
        # system slot is untouched, so the base prompt is still the head,
        # byte-identical.
        prompt = ex._build_prompt(
            result.new_messages,
            [Message(role="user", parts=[TextPart(text="next turn")])],
        )
        sys_msg = prompt[0]
        assert sys_msg.role == "system"
        text = sys_msg.parts[0].text
        assert text.startswith(BASE + "\n\n"), f"the post-compaction prompt lost its head: {text[:80]!r}"
    finally:
        await session.aclose()
        await backend.aclose()
