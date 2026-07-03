"""Executors build the right ExecutionContext and render system prompts by surface.

Reuses the chat-executor fixture from ``tests.agent.test_executor`` (established
cross-module test-helper import pattern in this repo).
"""

from __future__ import annotations

import pytest

from tests.agent.test_executor import _FakeLLM, _build_executor

_WS_BLOCK = (
    "Base prompt.\n\n"
    "{% if ctx.surface == 'workspace' %}USE FILES {{ ctx.artifact_dir }}{% endif %}"
)


@pytest.mark.asyncio
async def test_chat_execution_context_surface() -> None:
    ex, *_ = await _build_executor(llm=_FakeLLM(scripts=[]), system_prompt=["hi"])
    assert ex._execution_context.surface == "chat"
    assert ex._execution_context.artifact_dir is None


@pytest.mark.asyncio
async def test_chat_build_prompt_omits_workspace_block() -> None:
    ex, *_ = await _build_executor(
        llm=_FakeLLM(scripts=[]), system_prompt=[_WS_BLOCK]
    )
    prompt = ex._build_prompt(history=[], new_messages=[])
    sys_text = prompt[0].parts[0].text
    assert "Base prompt." in sys_text
    assert "USE FILES" not in sys_text
