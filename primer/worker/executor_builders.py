"""Executor / invocation-services builders for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). Each
function takes the :class:`~primer.worker.pool.WorkerPool` instance as ``pool``
and reads the same bound deps (``pool._storage`` / ``pool._provider_registry``
/ ``pool._approval_resolver`` / ...) the original methods did. The pool keeps
thin delegating methods (``WorkerPool._build_executor`` etc.) so call sites and
test monkeypatches continue to resolve through the instance - when one builder
calls another it goes through ``pool._build_X`` so patching ``pool._build_X``
still takes effect.

The per-kind executor / LLM / toolset imports stay lazy inside each function so
importing this module (and ``pool``) does not pull the executor + LLM
dependency tree at startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.model.workspace_session import WorkspaceSession, SessionStatus
from primer.worker.pool import _toolset_ids_from_scoped

if TYPE_CHECKING:
    from primer.worker.pool import WorkerPool


async def build_executor(pool: "WorkerPool", session: WorkspaceSession, workspace):
    """Construct an executor for ``session`` against ``workspace``.

    Dispatches on ``session.binding.kind``:

    * ``'agent'``  -> :class:`WorkspaceAgentExecutor` driving the
      on-disk :class:`AgentSession` allocated at create time.
    * ``'graph'``  -> :class:`WorkspaceGraphExecutor` (deferred -
      see :meth:`_build_graph_executor`).

    Imports happen lazily inside the per-kind branch so this module
    doesn't pull executor + LLM dependencies at startup.
    """
    if session.binding.kind == "agent":
        return await pool._build_agent_executor(session, workspace)
    if session.binding.kind == "graph":
        return await pool._build_graph_executor(session, workspace)
    raise ValueError(
        f"unknown session binding kind: {session.binding.kind!r}"
    )


async def build_session_executor(pool: "WorkerPool", session: WorkspaceSession):
    """Callable passed as ``SessionDispatchDeps.build_executor``.

    Resolves the workspace for ``session.workspace_id`` then delegates
    to :meth:`_build_executor`. The dispatch path consumes the
    executor's streaming ``invoke()`` via ``async for``, so we
    unwrap the legacy ``_TurnDriver``/``_GraphTurnDriver`` shim
    (which exposes ``invoke`` as a non-iterable coroutine for the
    old ``_run_one_turn`` path) and return the underlying streaming
    executor.
    """
    workspace = await pool._load_workspace_for_persist(session.workspace_id)
    wrapped = await pool._build_executor(session, workspace)
    inner = getattr(wrapped, "_executor", None)
    return inner if inner is not None else wrapped


def build_graph_invocation_services(
    pool: "WorkerPool", *, workspace, workspace_session, graph_session_id: str,
):
    """Build the GraphInvocationServices bundle for invoke_graph, or None
    when this workspace can't host a child graph executor (no state_repo /
    no holder session). Mirrors the per-node resolvers in
    _build_graph_executor so an invoked graph nests under the session's
    state with full parity (routers, approvals, subgraphs)."""
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.graph.invoke_graph import GraphInvocationServices
    from primer.graph.workspace_executor import WorkspaceGraphExecutor
    from primer.model.agent import Agent
    from primer.model.except_ import NotFoundError
    from primer.model.graph import Graph

    state_repo = getattr(workspace, "state_repo", None)
    if state_repo is None or workspace_session is None:
        return None

    async def agent_resolver(agent_id: str):
        row = await pool._storage.get_storage(Agent).get(agent_id)
        if row is None:
            raise NotFoundError(f"Agent {agent_id!r} not found")
        return row

    async def llm_resolver(agent):
        llm = await pool._provider_registry.get_llm(agent.model.provider_id)
        llm_model = await pool._resolve_llm_model(agent)
        return llm, llm_model

    async def tool_manager_resolver(agent):
        toolset_ids = _toolset_ids_from_scoped(agent.tools)
        toolset_providers: dict = {}
        for tid in toolset_ids:
            toolset_providers[tid] = await pool._provider_registry.get_toolset(tid)
        return ToolExecutionManager.for_workspace(
            toolset_providers=toolset_providers,
            session=workspace_session,
            approval_resolver=pool._approval_resolver,
            provider_registry=pool._provider_registry,
            tools=agent.tools,
        )

    async def graph_resolver(graph_id: str):
        row = await pool._storage.get_storage(Graph).get(graph_id)
        if row is None:
            raise NotFoundError(f"Graph {graph_id!r} not found")
        return row

    async def toolset_resolver(toolset_id: str):
        return await pool._provider_registry.get_toolset(toolset_id)

    router_registry = getattr(pool, "_router_registry", None)

    async def build_child_executor(*, graph, gsid: str):
        return WorkspaceGraphExecutor(
            graph=graph,
            agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,
            tool_manager_resolver=tool_manager_resolver,
            state_repo=state_repo,
            graph_session_id=gsid,
            workspace_session=workspace_session,
            graph_resolver=graph_resolver,
            router_registry=router_registry,
            principal=None,
            owns_session_lifecycle=False,
            toolset_resolver=toolset_resolver,
            approval_resolver=pool._approval_resolver,
            max_parallel_nodes=pool.config.max_parallel_nodes,
        )

    return GraphInvocationServices(
        resolve_graph=graph_resolver,
        build_child_executor=build_child_executor,
        session_id=workspace_session.session_id,
        workspace_id=workspace_session.workspace_id,
        graph_session_id=graph_session_id,
    )


async def build_agent_executor(pool: "WorkerPool", session: WorkspaceSession, workspace):
    """Build a turn-driver around :class:`WorkspaceAgentExecutor`.

    Resolves the agent definition (snapshot first, falls back to
    storage), the LLM via the provider registry, every toolset the
    agent registered, and the on-disk :class:`AgentSession` slot
    the API allocated at create time (id = ``session.id``).

    Returns a small adapter (not the executor itself) that exposes
    an awaitable ``invoke(messages)`` and a ``last_done_reason``
    attribute. The adapter consumes the executor's async-generator
    ``invoke`` to completion so the worker can await it as a single
    coroutine rather than iterating the stream directly.
    """
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.agent.workspace_executor import WorkspaceAgentExecutor
    from primer.model.agent import Agent
    from primer.model.except_ import NotFoundError
    from primer.worker.pool import _TurnDriver

    binding = session.binding  # AgentSessionBinding
    # Resolve the Agent: prefer the snapshot if the API froze one
    # at create time, otherwise look up the live row.
    agent = binding.agent_snapshot
    if agent is None:
        agent_storage = pool._storage.get_storage(Agent)
        agent = await agent_storage.get(binding.agent_id)
        if agent is None:
            raise NotFoundError(
                f"Agent {binding.agent_id!r} not found for session "
                f"{session.id!r}"
            )

    # Resolve the LLM adapter via the provider registry (cached).
    llm = await pool._provider_registry.get_llm(agent.model.provider_id)

    # Resolve the LLMModel (provider's config row carries the
    # context_length); used by the compaction strategy. The agent's
    # ``model.model_name`` is the provider-side identifier.
    llm_model = await pool._resolve_llm_model(agent)

    # agent.tools holds scoped tool ids (toolset_id__tool_name).
    # Derive the unique toolset prefixes so we only resolve the
    # toolset providers the agent actually needs.
    toolset_ids = _toolset_ids_from_scoped(agent.tools)
    toolset_providers: dict = {}
    for toolset_id in toolset_ids:
        provider = await pool._provider_registry.get_toolset(toolset_id)
        toolset_providers[toolset_id] = provider

    # Get the on-disk AgentSession the API allocated at create
    # time (Wave 2). The id matches session.id.
    agent_session = await workspace.get_session(session.id)
    if agent_session is None:
        raise NotFoundError(
            f"On-disk session slot for {session.id!r} missing on "
            f"workspace {workspace.id!r}; was it allocated via "
            "Workspace.start_session(..., id=sid)?"
        )

    # Build a workspace-aware ToolExecutionManager. The factory
    # composes the agent's tool surface with the session's
    # workspace tools and binds them to this AgentSession. The
    # ``tools`` list is the agent's scoped-tool surface - the
    # manager exposes exactly those tools to the LLM and rejects
    # dispatch on anything else.
    gis = pool._build_graph_invocation_services(
        workspace=workspace,
        workspace_session=agent_session,
        graph_session_id=session.id,
    )
    tool_manager = ToolExecutionManager.for_workspace(
        toolset_providers=toolset_providers,
        session=agent_session,
        approval_resolver=pool._approval_resolver,
        provider_registry=pool._provider_registry,
        tools=agent.tools,
        graph_invocation_services=gis,
    )

    from primer.agent.inform import SessionInformSink
    tool_manager.set_inform_sink(SessionInformSink(
        dispatcher=pool._channel_dispatcher,
        workspace_id=agent_session.workspace_id,
        session_id=agent_session.session_id,
        session=session,
        workspace_registry=pool._workspace_registry,
        artifact_registry=pool._artifact_storage_registry,
    ))

    executor = WorkspaceAgentExecutor(
        agent=agent,
        llm=llm,
        llm_model=llm_model,
        tool_manager=tool_manager,
        session=agent_session,
    )
    return _TurnDriver(executor)


async def build_graph_executor(pool: "WorkerPool", session: WorkspaceSession, workspace):
    """Build a turn-driver around :class:`WorkspaceGraphExecutor`.

    Resolves the graph (snapshot first, falls back to storage),
    the per-node agent + LLM + toolset resolvers (which mirror the
    agent path), the workspace's git-backed state repo (required -
    only :class:`primer.workspace.local.LocalWorkspace` exposes
    one today; sandbox/container/k8s backends will need StateRepo
    parity before they can host graph dispatch), and an optional
    :class:`RouterRegistry` stashed on app.state at startup.

    Unlike the agent path, the graph executor runs the WHOLE
    graph in one ``invoke()`` call. The returned :class:`_GraphTurnDriver`
    always reports ``last_done_reason = "graph_ended"`` so the
    post-turn status mapper transitions the session straight to
    ``ENDED`` - no re-enqueue.

    Phase 2 scope:
        - graph_resolver wired - subgraph nodes resolve from storage
        - router_registry wired from app.state (None if no
          callable routers registered -> callable-router edges raise)
        - workspace_session wired from the graph-holder slot
          allocated by POST /workspaces/{id}/sessions; agents in
          the graph receive composite system prompt augmentation
          + workspace tools per-node. Falls back to None for
          legacy graph-bound sessions created before the holder
          allocation landed.
    """
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.graph.workspace_executor import WorkspaceGraphExecutor
    from primer.model.agent import Agent
    from primer.model.except_ import ConfigError, NotFoundError
    from primer.model.graph import Graph
    from primer.worker.pool import _GraphTurnDriver

    binding = session.binding  # GraphSessionBinding

    # (1) Resolve the Graph: snapshot first, then storage. Falls back
    # gracefully so the executor sees a consistent definition even
    # if the row is edited mid-session.
    graph = binding.graph_snapshot
    if graph is None:
        graph_storage = pool._storage.get_storage(Graph)
        graph = await graph_storage.get(binding.graph_id)
        if graph is None:
            raise NotFoundError(
                f"Graph {binding.graph_id!r} not found for session "
                f"{session.id!r}"
            )

    # (2) Workspace state-repo: required for the executor's git-backed
    # state persistence. Only LocalWorkspace exposes one today.
    # getattr-with-default tolerates legacy fakes that predate the
    # state_repo addition to the ABC.
    state_repo = getattr(workspace, "state_repo", None)
    if state_repo is None:
        raise ConfigError(
            f"workspace {workspace.id!r} ({type(workspace).__name__}) "
            "does not expose a state_repo; graph-bound sessions "
            "require a workspace with StateRepo support "
            "(LocalWorkspace or SandboxWorkspace)."
        )

    # (3) Per-node resolvers - closures over self so each resolver
    # can use the same provider/storage caches as the agent path.

    async def agent_resolver(agent_id: str):
        agent_storage = pool._storage.get_storage(Agent)
        row = await agent_storage.get(agent_id)
        if row is None:
            raise NotFoundError(
                f"Agent {agent_id!r} referenced by graph "
                f"{graph.id!r} not found"
            )
        return row

    async def llm_resolver(agent):
        llm = await pool._provider_registry.get_llm(
            agent.model.provider_id
        )
        llm_model = await pool._resolve_llm_model(agent)
        return llm, llm_model

    # (4) Holder AgentSession allocated by POST /workspaces/{id}/sessions
    # (Phase 2). Optional - fall back to None for legacy graph-
    # bound sessions that were created before holder allocation
    # landed. With the holder, agents in the graph get composite
    # system prompt augmentation + workspace tools per-node.
    workspace_session = await workspace.get_session(session.id)

    async def tool_manager_resolver(agent):
        toolset_ids = _toolset_ids_from_scoped(agent.tools)
        toolset_providers: dict = {}
        for toolset_id in toolset_ids:
            provider = await pool._provider_registry.get_toolset(
                toolset_id
            )
            toolset_providers[toolset_id] = provider
        if workspace_session is not None:
            gis = pool._build_graph_invocation_services(
                workspace=workspace,
                workspace_session=workspace_session,
                graph_session_id=session.id,
            )
            return ToolExecutionManager.for_workspace(
                toolset_providers=toolset_providers,
                session=workspace_session,
                approval_resolver=pool._approval_resolver,
                provider_registry=pool._provider_registry,
                tools=agent.tools,
                graph_invocation_services=gis,
            )
        return ToolExecutionManager(
            toolset_providers=toolset_providers,
            approval_resolver=pool._approval_resolver,
            provider_registry=pool._provider_registry,
            tools=agent.tools,
        )

    # (4) Optional handles wired in later phases.

    async def graph_resolver(subgraph_id: str):
        graph_storage = pool._storage.get_storage(Graph)
        row = await graph_storage.get(subgraph_id)
        if row is None:
            raise NotFoundError(
                f"Subgraph {subgraph_id!r} referenced by graph "
                f"{graph.id!r} not found"
            )
        return row

    # RouterRegistry singleton stashed on app.state at startup
    # (None if no callables registered). Pass through; the
    # executor only needs it for _CallableRouter edges.
    router_registry = getattr(pool, "_router_registry", None)

    # Structured graph input is persisted on the session row by the
    # session-create handler as ``session.metadata['graph_input']``.
    # Relay it into the executor so Begin materialises its NodeOutput
    # from it and per-node templates (e.g. ``{{ initial_input.task }}``)
    # render against the structured value. Without this the executor
    # falls back to the (empty) messages list and any node reading a
    # field of ``initial_input`` fails to render.
    graph_input = (session.metadata or {}).get("graph_input")

    # Resolve a toolset_id -> provider so tool_call nodes can invoke
    # internal-toolset tools (web__web_search, system__...), not just
    # workspace tools. Mirrors the agent path's per-toolset resolution.
    async def toolset_resolver(toolset_id: str):
        return await pool._provider_registry.get_toolset(toolset_id)

    executor = WorkspaceGraphExecutor(
        graph=graph,
        agent_resolver=agent_resolver,
        llm_resolver=llm_resolver,
        tool_manager_resolver=tool_manager_resolver,
        state_repo=state_repo,
        graph_session_id=session.id,
        workspace_session=workspace_session,
        graph_resolver=graph_resolver,
        router_registry=router_registry,
        graph_input=graph_input,
        principal=None,
        owns_session_lifecycle=True,
        toolset_resolver=toolset_resolver,
        approval_resolver=pool._approval_resolver,
        max_parallel_nodes=pool.config.max_parallel_nodes,
    )
    return _GraphTurnDriver(executor)


async def resolve_llm_model(pool: "WorkerPool", agent):
    """Look up the :class:`LLMModel` row matching ``agent.model``.

    Walks the configured :class:`LLMProvider`'s ``models`` list and
    returns the entry whose ``name`` matches ``agent.model.model_name``.
    Raises :class:`ConfigError` if the provider doesn't list the
    requested model name.
    """
    from primer.model.except_ import ConfigError, NotFoundError
    from primer.model.provider import LLMProvider

    provider_storage = pool._storage.get_storage(LLMProvider)
    provider_row = await provider_storage.get(agent.model.provider_id)
    if provider_row is None:
        raise NotFoundError(
            f"LLMProvider {agent.model.provider_id!r} not found "
            f"for agent {agent.id!r}"
        )
    for m in provider_row.models:
        if m.name == agent.model.model_name:
            return m
    raise ConfigError(
        f"LLMProvider {agent.model.provider_id!r} does not list "
        f"model {agent.model.model_name!r}; configured models: "
        f"{[m.name for m in provider_row.models]}"
    )


def infer_post_turn_status(
    pool: "WorkerPool", executor, session: WorkspaceSession,
) -> SessionStatus:
    """Map the executor's last ``Done.stop_reason`` to a SessionStatus.

    :class:`WorkspaceAgentExecutor` exposes the trailing stop reason
    via :attr:`last_done_reason` (set after each ``invoke`` call).
    The mapping mirrors what the executor itself decides for the
    cases it handles:

    * ``'end_turn'`` / ``'stop'`` / ``'stop_sequence'`` -> RUNNING
      (more user-driven turns may follow).
    * ``'tool_use'`` -> RUNNING (next turn dispatches tools).
    * ``'max_tokens'`` / ``'error'`` / ``'content_filter'`` ->
      WAITING (operator inspection needed).
    * ``None`` (e.g. fake test executors that never iterate) ->
      RUNNING (default; preserves the legacy behaviour).

    Workspace-side WAITING transitions for explicit waits
    (user-input prompt heuristic, tool-approval hand-off) are set
    INSIDE :meth:`WorkspaceAgentExecutor.invoke` via
    :meth:`AgentSession.set_status`. The post-turn re-read here
    only handles cases where the executor exited cleanly without
    having taken a wait.
    """
    last_reason = getattr(executor, "last_done_reason", None)
    # Graph dispatch sets a sentinel - the graph executor runs the
    # whole graph in one invoke() call, so there's no follow-up
    # turn for the worker to schedule.
    if last_reason == "graph_ended":
        return SessionStatus.ENDED
    if last_reason in ("max_tokens", "error", "content_filter"):
        return SessionStatus.WAITING
    return SessionStatus.RUNNING
