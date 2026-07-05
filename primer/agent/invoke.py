"""Shared subagent invocation: a one-shot agent runner + a process-wide
invocation-depth guard reused by the invoke_agent and invoke_graph tools.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from typing import TYPE_CHECKING, Any

from primer.model.chat import Message, TextPart


if TYPE_CHECKING:
    from primer.model.principal import PrincipalRef


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


@contextlib.contextmanager
def _depth_set(depth: int):
    """Pin the invocation depth contextvar to ``depth`` for the duration.

    Used by ``resume_subagent``: a resumed frame must keep counting from the
    depth it was parked at (so nested re-entry keeps loop-guarding) rather than
    restarting at zero. Restores the prior depth on exit.
    """
    token = _DEPTH.set(depth)
    try:
        yield
    finally:
        _DEPTH.reset(token)


async def build_subagent_toolmanager(
    context: Any,
    *,
    storage_provider: Any = None,
    provider_registry: Any,
    approval_resolver: Any | None = None,
) -> Any:
    """Build the :class:`ToolExecutionManager` for a subagent turn.

    Resolves the toolset providers backing ``context.tools`` from
    ``provider_registry`` and wires the manager exactly as the worker wires a
    normal turn: the approval gate (``approval_resolver`` + ``provider_registry``
    so the gate enforces session-scoped approvals) and a session-identity shim
    (``_SubagentSession``) derived from ``context`` so the gate's event key and
    the yielding-tool ToolContext are scoped to the inherited session. When the
    context carries no ``session_id`` the manager binds ``chat_id`` instead,
    mirroring the chat path. The full ``context.tools`` surface is passed
    UNFILTERED so yielding tools can park.

    ``context`` is an :class:`~primer.worker.frames.AgentResumeContext`
    (``session_id``, ``workspace_id``, ``chat_id``, ``principal``, ``tools``).
    ``storage_provider`` is accepted for call-site symmetry but unused here.

    Shared by :func:`run_subagent` (initial dispatch), :func:`resume_subagent`
    (rebuild-and-continue), and ``frames.apply_leaf`` (re-dispatch of an
    approved original_call), so the manager wiring lives in exactly one place.
    """
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.worker.pool import _toolset_ids_from_scoped

    tools = list(context.tools or [])
    toolset_ids = _toolset_ids_from_scoped(tools)
    toolset_providers: dict[str, Any] = {}
    for tid in toolset_ids:
        toolset_providers[tid] = await provider_registry.get_toolset(tid)

    session_id = context.session_id
    subagent_session = (
        _SubagentSession(
            session_id=session_id,
            workspace_id=context.workspace_id,
            agent_id=getattr(context, "agent_id", None) or "subagent",
        )
        if session_id is not None
        else None
    )
    return ToolExecutionManager(
        toolset_providers=toolset_providers,
        workspace_session=subagent_session,
        approval_resolver=approval_resolver,
        provider_registry=provider_registry,
        tools=tools,
        chat_id=context.chat_id if subagent_session is None else None,
    )


def _push_agent_frame_on_yield(
    yld: Any,
    *,
    agent_id: str,
    produced: list,
    invoke_tool_call_id: str | None,
    depth: int,
    context: Any,
) -> None:
    """Prepend a fresh :class:`AgentFrame` onto ``yld.frames`` (in place).

    Captures the in-progress turn (``produced``, serialised to ``list[dict]``)
    plus the ids/context needed to rebuild the agent, ahead of any frames a
    deeper invocation already pushed. Shared by :func:`run_subagent` and
    :func:`resume_subagent` so the yield-to-frame wiring lives in one place.
    """
    from primer.worker.frames import AgentFrame

    existing = list(getattr(yld, "frames", []) or [])
    frame = AgentFrame(
        agent_id=agent_id,
        llm_messages=[m.model_dump(mode="json") for m in produced],
        tool_call_id=invoke_tool_call_id,
        depth=depth,
        context=context,
    )
    yld.frames = [frame] + existing


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
    identity: "PrincipalRef | None" = None,
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
    from primer.model.yield_ import YieldToWorker
    from primer.worker.frames import AgentResumeContext

    agent, llm, llm_model = await _resolve_agent_runtime(
        agent_id, storage_provider=storage_provider,
        provider_registry=provider_registry,
    )

    # Wire the manager the same way the worker wires a normal turn (the
    # approval gate + provider_registry + a session-identity shim, with the
    # full agent.tools surface UNFILTERED so yielding tools can park). The
    # context carries the inherited ids: build_subagent_toolmanager derives
    # the shim (or the chat binding) from it. Shared with resume_subagent and
    # frames.apply_leaf so the manager wiring lives in exactly one place.
    context = AgentResumeContext(
        session_id=session_id,
        workspace_id=workspace_id,
        chat_id=chat_id,
        principal=principal,
        tools=list(agent.tools),
    )
    tool_manager = await build_subagent_toolmanager(
        context,
        storage_provider=storage_provider,
        provider_registry=provider_registry,
        approval_resolver=approval_resolver,
    )

    from primer.model.graph import build_execution_context
    from primer.agent.prompt_render import render_system_prompt_or_raw

    surface = "workspace" if workspace_id else ("chat" if chat_id else "memory")
    _sub_ctx = build_execution_context(
        surface=surface,
        workspace_id=workspace_id,
        session_id=session_id,
        identity=identity,
    )
    sys_text = (
        render_system_prompt_or_raw(agent.system_prompt, _sub_ctx)
        if agent.system_prompt else ""
    )
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
        _push_agent_frame_on_yield(
            yld,
            agent_id=agent_id,
            produced=produced,
            invoke_tool_call_id=invoke_tool_call_id,
            depth=_DEPTH.get(),
            context=context,
        )
        raise

    return _final_assistant_text(produced)


async def _resolve_agent_runtime(
    agent_id: str,
    *,
    storage_provider: Any,
    provider_registry: Any,
) -> tuple[Any, Any, Any]:
    """Resolve ``(agent, llm, llm_model)`` for a subagent run.

    Shared by :func:`run_subagent` and :func:`resume_subagent` so both
    paths resolve the agent definition, its LLM client and the concrete
    model row identically. Raises ``ValueError`` if the agent or its model
    cannot be found.
    """
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
    return agent, llm, llm_model


def _final_assistant_text(produced: list[Message]) -> str:
    """Extract the final assistant message's text from a turn's output buffer.

    Mirrors the text extraction shared by :func:`run_subagent` and
    :func:`resume_subagent`: the last assistant message's ``TextPart``s,
    joined.
    """
    texts: list[str] = []
    for m in reversed(produced):
        if m.role == "assistant":
            texts = [p.text for p in m.parts if isinstance(p, TextPart)]
            break
    return "".join(texts)


async def resume_subagent(
    *,
    agent_id: str,
    context: Any,
    llm_messages: list[dict[str, Any]],
    child_result: Any,
    depth: int,
    storage_provider: Any,
    provider_registry: Any,
    approval_resolver: Any | None = None,
    invoke_tool_call_id: str | None = None,
) -> str:
    """Resume a parked subagent turn and return its final assistant text.

    Resume-by-rerun: rather than rehydrating a stateful executor, we rebuild
    the agent runtime + tool manager and re-run a single turn whose prompt is
    the parked mid-flight history followed by the now-completed child's tool
    result. The LLM continues past the tool call exactly as if the result had
    arrived synchronously.

    Parameters mirror an :class:`~primer.worker.frames.AgentFrame`'s payload:
    ``llm_messages`` is the serialised mid-flight history (``list[dict]``),
    ``child_result`` is the resolved tool result for the call that parked,
    ``depth`` is the nesting depth this frame was parked at, and ``context``
    is the :class:`~primer.worker.frames.AgentResumeContext` carrying the
    inherited ids + tool surface.

    The run is pinned to ``depth`` (via ``_depth_set``) so nested re-entry
    keeps counting from where it parked. If the continuation yields again, a
    FRESH :class:`AgentFrame` (carrying the new in-progress history) is
    prepended onto the yield's ``frames`` stack and the yield is re-raised,
    so the worker can re-park the whole chain.
    """
    from primer.agent.loop import run_agent_turn
    from primer.model.chat import Message
    from primer.model.yield_ import YieldToWorker

    agent, llm, llm_model = await _resolve_agent_runtime(
        agent_id, storage_provider=storage_provider,
        provider_registry=provider_registry,
    )

    tool_manager = await build_subagent_toolmanager(
        context,
        storage_provider=storage_provider,
        provider_registry=provider_registry,
        approval_resolver=approval_resolver,
    )

    rehydrated = [Message.model_validate(m) for m in (llm_messages or [])]
    resume_prompt = rehydrated + [Message(role="tool", parts=[child_result])]

    produced: list[Message] = []
    try:
        with _depth_set(depth):
            async for _ev in run_agent_turn(
                agent=agent, llm=llm, llm_model=llm_model,
                tool_manager=tool_manager, prompt=resume_prompt,
                principal=context.principal, messages_out=produced,
            ):
                pass
    except YieldToWorker as yld:
        _push_agent_frame_on_yield(
            yld,
            agent_id=agent_id,
            produced=produced,
            invoke_tool_call_id=invoke_tool_call_id,
            depth=depth,
            context=context,
        )
        raise

    return _final_assistant_text(produced)
