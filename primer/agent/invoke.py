"""Shared subagent invocation: a one-shot agent runner + a process-wide
invocation-depth guard reused by the invoke_agent and invoke_graph tools.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from typing import Any

from primer.model.chat import Message, TextPart


MAX_INVOCATION_DEPTH = int(os.environ.get("PRIMER_MAX_INVOCATION_DEPTH", "8"))

_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "primer_invocation_depth", default=0,
)


class InvocationDepthExceeded(Exception):
    """Raised when nested invoke_agent / invoke_graph exceeds the max depth."""


@contextlib.contextmanager
def invocation_depth_guard():
    """Increment the invocation depth for the duration of a nested run; raise
    InvocationDepthExceeded if it would exceed MAX_INVOCATION_DEPTH."""
    depth = _DEPTH.get()
    if depth >= MAX_INVOCATION_DEPTH:
        raise InvocationDepthExceeded(
            f"invocation depth {depth} would exceed max {MAX_INVOCATION_DEPTH}"
        )
    token = _DEPTH.set(depth + 1)
    try:
        yield
    finally:
        _DEPTH.reset(token)


async def run_subagent(
    *,
    agent_id: str,
    prompt: str,
    storage_provider: Any,
    provider_registry: Any,
    principal: str | None = None,
) -> str:
    """Run agent ``agent_id`` once on ``prompt`` (stateless: system prompt +
    prompt, no shared history), with its non-yielding toolset tools, and return
    the final assistant text. Wrap calls in ``invocation_depth_guard()``."""
    from primer.agent.loop import run_agent_turn
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.worker.pool import _toolset_ids_from_scoped
    from primer.model.agent import Agent
    from primer.model.provider import LLMProvider

    agents = storage_provider.get_storage(Agent)
    agent = await agents.get(agent_id)
    if agent is None:
        raise ValueError(f"agent {agent_id!r} does not exist")

    llm = await provider_registry.get_llm(agent.model.provider_id)
    provider_rows = storage_provider.get_storage(LLMProvider)
    provider_row = await provider_rows.get(agent.model.provider_id)
    llm_model = next(
        (m for m in (provider_row.models if provider_row else [])
         if m.name == agent.model.model_name), None,
    )
    if llm_model is None:
        raise ValueError(
            f"model {agent.model.model_name!r} not found on provider "
            f"{agent.model.provider_id!r}"
        )

    toolset_ids = _toolset_ids_from_scoped(agent.tools)
    toolset_providers: dict[str, Any] = {}
    for tid in toolset_ids:
        toolset_providers[tid] = await provider_registry.get_toolset(tid)
    non_yielding: list[str] = []
    for scoped in agent.tools:
        # Split on the LAST "__": bare tool ids can't contain it, but toolset
        # ids can (e.g. operator/MCP-named) - mirror _toolset_ids_from_scoped's
        # rsplit so the toolset_providers key matches.
        tid, _, bare = scoped.rpartition("__")
        prov = toolset_providers.get(tid)
        if prov is not None and not prov.is_yielding(bare):
            non_yielding.append(scoped)
    tool_manager = ToolExecutionManager(
        toolset_providers=toolset_providers,
        tools=non_yielding,
    )

    sys_text = "\n\n".join(agent.system_prompt) if agent.system_prompt else ""
    prompt_msgs: list[Message] = []
    if sys_text:
        prompt_msgs.append(Message(role="system", parts=[TextPart(text=sys_text)]))
    prompt_msgs.append(Message(role="user", parts=[TextPart(text=prompt)]))

    produced: list[Message] = []
    async for _ev in run_agent_turn(
        agent=agent, llm=llm, llm_model=llm_model, tool_manager=tool_manager,
        prompt=prompt_msgs, principal=principal, messages_out=produced,
    ):
        pass

    texts: list[str] = []
    for m in reversed(produced):
        if m.role == "assistant":
            texts = [p.text for p in m.parts if isinstance(p, TextPart)]
            break
    return "".join(texts)
