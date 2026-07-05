"""End-to-end guard (Task 6, Spec §8.4): the COMPOSITE workspace system
prompt — the base :class:`Agent`'s ``system_prompt`` fragments PLUS the
session's ``system_prompt_fragment`` — renders as a whole through the
identity-aware :func:`render_system_prompt_or_raw`, not just the base
fragment alone.

``WorkspaceAgentExecutor.__init__`` (``primer/agent/workspace_executor.py``
:72-99) rebuilds a ``composite_agent`` whose ``system_prompt`` is
``list(agent.system_prompt) + [session.system_prompt_fragment]`` and hands
it to ``_BaseAgentExecutor.__init__``; ``_BaseAgentExecutor._build_prompt``
(``primer/agent/base.py``:337) then renders ``self._agent.system_prompt``
(the WHOLE composite) against ``self._execution_context``, which carries
``identity`` (T3). This test drives one real ``invoke()`` turn and inspects
the system message the (fake) LLM actually received, proving both halves of
the composite made it through the identity-carrying render together.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Done, Message, TextDelta, TextPart
from primer.model.principal import PrincipalRef
from tests.agent.test_workspace_executor import (
    _FakeLLM,
    _agent,
    _build_session,
    _model,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


@pytest.mark.asyncio
async def test_composite_system_prompt_renders_identity(tmp_path: Path) -> None:
    backend, _, session = await _build_session(tmp_path)
    try:
        ref = PrincipalRef(
            type="trigger",
            id="t-1",
            display="nightly",
            role=None,
            source="internal",
        )
        llm = _FakeLLM(
            scripts=[
                [
                    TextDelta(text="ok", index=0),
                    Done(stop_reason="stop", raw_reason="stop"),
                ]
            ]
        )
        mgr = ToolExecutionManager.for_workspace(
            toolset_providers={}, session=session
        )
        executor = WorkspaceAgentExecutor(
            agent=_agent(system_prompt=["actor={{ ctx.identity.type }}"]),
            llm=llm,  # type: ignore[arg-type]
            llm_model=_model(),
            tool_manager=mgr,
            session=session,
            identity=ref,
        )
        async for _ in executor.invoke(
            [Message(role="user", parts=[TextPart(text="hi")])]
        ):
            pass

        sys_msg = llm.calls[0]["messages"][0]
        assert sys_msg.role == "system"
        sys_text = sys_msg.parts[0].text
        # Base-agent fragment rendered against the identity-carrying ctx.
        assert "actor=trigger" in sys_text
        # Workspace fragment (appended by the composite builder) present too
        # -- proves the WHOLE composite reached the render, not just the base.
        assert session.session_id in sys_text
    finally:
        await session.aclose()
        await backend.aclose()
