"""ChatTurnRunner builds/consumes an ExecutionContext when rendering the
agent's system prompt (Layer 3 Task 5, spec §8.4).

Mirrors ``tests/agent/test_executor_execution_context.py`` for the chat
surface: ``ctx.identity`` must be reachable from a chat agent's
``system_prompt`` template, and a marker-free prompt must still render
byte-identical to the old raw ``"\\n\\n".join(...)``.
"""

from __future__ import annotations

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, TextPart
from primer.model.chats import Chat, ChatMessage
from primer.model.graph import build_execution_context
from primer.model.principal import PrincipalRef
from primer.model.provider import LLMModel


def _runner(*, system_prompt: list[str], execution_context=None) -> ChatTurnRunner:
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
        system_prompt=system_prompt,
    )
    return ChatTurnRunner(
        agent=agent,
        llm=None,
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=None,
        chat_storage=None,
        message_storage=None,
        execution_context=execution_context,
    )


def test_build_prompt_renders_ctx_identity() -> None:
    ctx = build_execution_context(
        surface="chat",
        identity=PrincipalRef(
            type="user", id="u-1", display="alice", role="user", source="local",
        ),
    )
    runner = _runner(
        system_prompt=["I am {{ ctx.identity.display }}"], execution_context=ctx,
    )
    new_msg = Message(role="user", parts=[TextPart(text="hi")])
    prompt = runner._build_prompt(history=[], new_user_msg=new_msg)
    sys_text = prompt[0].parts[0].text
    assert sys_text == "I am alice"


def test_build_prompt_marker_free_is_byte_identical() -> None:
    fragments = ["line one.", "line two."]
    runner = _runner(
        system_prompt=fragments,
        execution_context=build_execution_context(surface="chat"),
    )
    new_msg = Message(role="user", parts=[TextPart(text="hi")])
    prompt = runner._build_prompt(history=[], new_user_msg=new_msg)
    sys_text = prompt[0].parts[0].text
    assert sys_text == "\n\n".join(fragments)


def test_execution_context_defaults_to_chat_surface_when_omitted() -> None:
    """Legacy callers that don't pass ``execution_context`` still get a
    valid ``ExecutionContext`` (the compaction ``__new__`` path + any
    stray direct construction)."""
    runner = _runner(system_prompt=["hi"])
    assert runner._execution_context.surface == "chat"
