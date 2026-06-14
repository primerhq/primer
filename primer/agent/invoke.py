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


class _SubagentSession:
    """Minimal session-identity shim bound to a subagent's tool manager.

    The :class:`~primer.agent.tool_manager.ToolExecutionManager` derives the
    approval-gate event key and the yielding-tool ToolContext from its bound
    ``workspace_session`` (reading ``session_id`` / ``workspace_id`` /
    ``agent_id``). A subagent invoked via ``system__invoke_agent`` runs WITHIN
    the parent's session but is not itself a workspace-bound session with
    workspace tools, so we hand the manager this lightweight stand-in carrying
    the inherited identity. ``workspace_tools`` is empty: the subagent's tool
    surface is its own ``agent.tools``, never the parent's workspace tools.
    """

    workspace_tools: list = []

    def __init__(
        self, *, session_id: str, workspace_id: str | None, agent_id: str
    ) -> None:
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.agent_id = agent_id


async def run_subagent(
    *,
    agent_id: str,
    prompt: str,
    storage_provider: Any,
    provider_registry: Any,
    principal: str | None = None,
    approval_resolver: Any | None = None,
    session_id: str | None = None,
    workspace_id: str | None = None,
    chat_id: str | None = None,
    invoke_tool_call_id: str | None = None,
) -> str:
    """Run agent ``agent_id`` once on ``prompt`` (stateless: system prompt +
    prompt, no shared history) and return the final assistant text.

    The subagent runs with its FULL ``agent.tools`` surface - including
    yielding tools - and the inherited approval gate. If a tool yields (an
    approval gate fires or a yielding tool parks), the in-progress turn's
    messages are captured into an :class:`AgentFrame` prepended onto the
    :class:`YieldToWorker`'s ``frames`` stack, and the exception is re-raised
    so the worker can park the whole nested-invocation chain. Wrap calls in
    ``invocation_depth_guard()``.
    """
    from primer.agent.loop import run_agent_turn
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.worker.pool import _toolset_ids_from_scoped
    from primer.model.agent import Agent
    from primer.model.provider import LLMProvider
    from primer.model.yield_ import YieldToWorker
    from primer.worker.frames import AgentFrame, AgentResumeContext

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

    # Wire the manager the same way the worker wires a normal turn (see
    # WorkerPool._build_agent_executor): pass the approval_resolver +
    # provider_registry so the gate enforces session-scoped approvals, and
    # bind a session identity so the gate's event key and the yielding-tool
    # ToolContext are scoped to the inherited (parent) session. When no
    # session_id is inherited (chat-surface or bare caller) fall back to
    # binding the chat_id, mirroring the chat path's manager. The full
    # ``agent.tools`` surface is passed UNFILTERED so yielding tools can park.
    subagent_session = (
        _SubagentSession(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent.id,
        )
        if session_id is not None
        else None
    )
    tool_manager = ToolExecutionManager(
        toolset_providers=toolset_providers,
        workspace_session=subagent_session,
        approval_resolver=approval_resolver,
        provider_registry=provider_registry,
        tools=list(agent.tools),
        chat_id=chat_id if subagent_session is None else None,
    )

    sys_text = "\n\n".join(agent.system_prompt) if agent.system_prompt else ""
    prompt_msgs: list[Message] = []
    if sys_text:
        prompt_msgs.append(Message(role="system", parts=[TextPart(text=sys_text)]))
    prompt_msgs.append(Message(role="user", parts=[TextPart(text=prompt)]))

    # ``produced`` is the authoritative in-progress conversation at the yield
    # point: run_agent_turn appends the assistant message that carried the
    # tool_use into this buffer BEFORE dispatching the tool (loop.py), and the
    # tool dispatch is what raises YieldToWorker. Unlike the base executor,
    # run_agent_turn does NOT stamp ``yld.llm_messages`` (no executor frame in
    # this path), so this buffer - not ``yld.llm_messages`` - is the source of
    # truth for the AgentFrame's mid-flight history.
    produced: list[Message] = []
    try:
        async for _ev in run_agent_turn(
            agent=agent, llm=llm, llm_model=llm_model, tool_manager=tool_manager,
            prompt=prompt_msgs, principal=principal, messages_out=produced,
        ):
            pass
    except YieldToWorker as yld:
        existing = list(getattr(yld, "frames", []) or [])
        frame = AgentFrame(
            agent_id=agent_id,
            llm_messages=[m.model_dump(mode="json") for m in produced],
            tool_call_id=invoke_tool_call_id,
            depth=_DEPTH.get(),
            context=AgentResumeContext(
                session_id=session_id,
                workspace_id=workspace_id,
                chat_id=chat_id,
                principal=principal,
                tools=list(agent.tools),
            ),
        )
        yld.frames = [frame] + existing
        raise

    texts: list[str] = []
    for m in reversed(produced):
        if m.role == "assistant":
            texts = [p.text for p in m.parts if isinstance(p, TextPart)]
            break
    return "".join(texts)
