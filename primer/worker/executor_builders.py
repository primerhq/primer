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

import logging
from typing import TYPE_CHECKING

from primer.model.principal import PrincipalRef
from primer.model.workspace_session import WorkspaceSession, SessionStatus
from primer.worker.pool import _toolset_ids_from_scoped


logger = logging.getLogger(__name__)


def _pool_event_recorder(pool):
    """Platform event recorder over the pool's storage + bus refs.

    None when the pool was built without storage (unit-test pools), so
    the tool manager simply skips tool.called emission.
    """
    storage = getattr(pool, "_storage", None)
    if storage is None:
        return None
    from primer.events.recorder import recorder_for

    return recorder_for(storage, getattr(pool, "_event_bus", None))

if TYPE_CHECKING:
    from primer.worker.pool import WorkerPool


def _external_defs_for(session_row, agent) -> "list | None":
    """Parse the row's stored external tool defs, gated by the agent flag.

    ``agent`` is the snapshot-first resolved Agent the builder already
    holds, so the gate matches the API-side registration gate. Returning
    None when the flag is off is defense in depth: the API already
    rejected registration, so a populated row with the flag off can only
    mean the agent definition changed mid-session.
    """
    raw = getattr(session_row, "external_tools", None)
    if not raw:
        return None
    if not getattr(agent, "allow_external_tools", False):
        return None
    from primer.model.external_tool import ExternalToolDef

    return [ExternalToolDef.model_validate(d) for d in raw]


async def client_tools_attached(pool: "WorkerPool", session_row) -> bool:
    """Does THIS turn carry the client toolset (S3 spec section 4)?

    Decided ONCE per turn. A parked row already carries the decision its
    turn started with, so a resume reuses it verbatim: without that, a
    park outliving the 30s attachment TTL would silently drop the toolset
    mid-turn and the resumed prompt would disagree with the parked one.
    """
    blob = session_row.parked_state or {}
    if "client_tools_attached" in blob:
        return bool(blob["client_tools_attached"])
    if pool._storage is None:
        return False
    from primer.model.client_attachment import ClientAttachment
    from primer.session.attachment import live_attachments

    live = await live_attachments(
        pool._storage.get_storage(ClientAttachment), session_row.id,
    )
    return bool(live)


async def client_toolset_for(pool: "WorkerPool", session_row) -> dict:
    """``{client: ClientToolsetProvider()}`` when attached, else ``{}``."""
    if not await client_tools_attached(pool, session_row):
        return {}
    from primer.toolset.client import CLIENT_TOOLSET_ID, ClientToolsetProvider

    return {CLIENT_TOOLSET_ID: ClientToolsetProvider()}


async def _resolve_default_artifact_storage(pool: "WorkerPool"):
    """The deployment's default ArtifactStorage, or ``None``.

    ``pool._artifact_storage_registry`` is optional (unset in some
    pool test configurations, same tolerance as ``pool._storage`` /
    ``pool._channel_dispatcher`` elsewhere in this module) -- None
    here means the turn's prompt is built without hydration, an
    explicit no-op (see ``hydrate_prompt_parts``), not a hard failure.
    """
    if pool._artifact_storage_registry is None:
        return None
    try:
        return await pool._artifact_storage_registry.get_default()
    except Exception:  # noqa: BLE001 -- degrade to no hydration
        logger.warning(
            "executor_builders: default artifact storage resolve failed",
            exc_info=True,
        )
        return None


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
    pool: "WorkerPool",
    *,
    workspace,
    workspace_session,
    graph_session_id: str,
    initiated_by: "PrincipalRef | None" = None,
):
    """Build the GraphInvocationServices bundle for invoke_graph, or None
    when this workspace can't host a child graph executor (no state_repo /
    no holder session). Mirrors the per-node resolvers in
    _build_graph_executor so an invoked graph nests under the session's
    state with full parity (routers, approvals, subgraphs).

    ``initiated_by`` is the enclosing (durable) session's attribution —
    threaded onto the child graph's ``ToolExecutionManager`` so a nested
    ``create_workspace_session`` call inherits the run's identity rather
    than falling back to the system principal."""
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

    async def llm_resolver(agent, profile_id: str | None = None):
        return await resolve_llm_and_model(pool, agent, profile_id)

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
            initiated_by=initiated_by,
            event_recorder=_pool_event_recorder(pool),
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
            identity=initiated_by,
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

    # Resolve the profile, then the adapter it names. The profile
    # carries the provider id, the provider-side model name, the
    # context_length compaction needs, and any API-level tunables.
    # binding.profile_id overrides the agent's default for THIS run; the
    # agent definition is unchanged and other sessions are unaffected.
    llm, llm_model = await resolve_llm_and_model(
        pool, agent, getattr(binding, "profile_id", None),
    )

    # agent.tools holds scoped tool ids (toolset_id__tool_name).
    # Derive the unique toolset prefixes so we only resolve the
    # toolset providers the agent actually needs.
    toolset_ids = _toolset_ids_from_scoped(agent.tools)
    toolset_providers: dict = {}
    for toolset_id in toolset_ids:
        provider = await pool._provider_registry.get_toolset(toolset_id)
        toolset_providers[toolset_id] = provider

    # Client tools ride the turn only while a browser has the session
    # open. Gated by attachment alone: allow_external_tools stays the
    # API-caller gate and says nothing about notifying client tools.
    toolset_providers.update(await client_toolset_for(pool, session))

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
    #
    # ``initiated_by`` propagates this session's own attribution onto the
    # ToolExecutionManager so a tool that spawns a child session
    # (create_workspace_session) inherits it instead of falling back to
    # the system principal. ``session.initiated_by`` is only absent for
    # historical rows created before this attribution landed.
    initiated_by = session.initiated_by or PrincipalRef.system()
    gis = pool._build_graph_invocation_services(
        workspace=workspace,
        workspace_session=agent_session,
        graph_session_id=session.id,
        initiated_by=initiated_by,
    )
    from primer.model.external_tool import ExternalToolCall

    tool_manager = ToolExecutionManager.for_workspace(
        toolset_providers=toolset_providers,
        session=agent_session,
        approval_resolver=pool._approval_resolver,
        provider_registry=pool._provider_registry,
        tools=agent.tools,
        graph_invocation_services=gis,
        initiated_by=initiated_by,
        event_recorder=_pool_event_recorder(pool),
        external_tools=_external_defs_for(session, agent),
        # ``pool._storage`` is always present in production; some pool
        # unit-tests construct the pool with ``storage=None`` (see the
        # same tolerance in WorkerPool._dispatch). Harmless to pass None:
        # ToolExecutionManager only builds the external dispatcher when
        # the agent actually has external tools, and an agent that has
        # them always came through a pool with storage.
        external_call_storage=(
            pool._storage.get_storage(ExternalToolCall)
            if pool._storage is not None
            else None
        ),
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
        identity=initiated_by,
        artifact_storage=await _resolve_default_artifact_storage(pool),
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

    async def llm_resolver(agent, profile_id: str | None = None):
        return await resolve_llm_and_model(pool, agent, profile_id)

    # (4) Holder AgentSession allocated by POST /workspaces/{id}/sessions
    # (Phase 2). Optional - fall back to None for legacy graph-
    # bound sessions that were created before holder allocation
    # landed. With the holder, agents in the graph get composite
    # system prompt augmentation + workspace tools per-node.
    workspace_session = await workspace.get_session(session.id)

    # This graph session's own attribution, propagated onto every
    # per-node ToolExecutionManager so a tool_call node's
    # create_workspace_session inherits it (see build_agent_executor).
    initiated_by = session.initiated_by or PrincipalRef.system()

    async def tool_manager_resolver(agent):
        toolset_ids = _toolset_ids_from_scoped(agent.tools)
        toolset_providers: dict = {}
        for toolset_id in toolset_ids:
            provider = await pool._provider_registry.get_toolset(
                toolset_id
            )
            toolset_providers[toolset_id] = provider
        # Invoker-supplied tools: the graph session's stored defs are
        # injected per NODE, gated by each node agent's flag (the
        # resolver receives the node's Agent). Rows for graph-node calls
        # carry node attribution in the checkpoint, not on the row.
        from primer.model.external_tool import ExternalToolCall

        node_external_tools = _external_defs_for(session, agent)
        node_call_storage = (
            pool._storage.get_storage(ExternalToolCall)
            if pool._storage is not None
            else None
        )
        if workspace_session is not None:
            gis = pool._build_graph_invocation_services(
                workspace=workspace,
                workspace_session=workspace_session,
                graph_session_id=session.id,
                initiated_by=initiated_by,
            )
            return ToolExecutionManager.for_workspace(
                toolset_providers=toolset_providers,
                session=workspace_session,
                approval_resolver=pool._approval_resolver,
                provider_registry=pool._provider_registry,
                tools=agent.tools,
                graph_invocation_services=gis,
                initiated_by=initiated_by,
                external_tools=node_external_tools,
                external_call_storage=node_call_storage,
                event_recorder=_pool_event_recorder(pool),
            )
        return ToolExecutionManager(
            toolset_providers=toolset_providers,
            approval_resolver=pool._approval_resolver,
            provider_registry=pool._provider_registry,
            tools=agent.tools,
            initiated_by=initiated_by,
            external_tools=node_external_tools,
            external_call_storage=node_call_storage,
            event_recorder=_pool_event_recorder(pool),
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
        identity=initiated_by,
        owns_session_lifecycle=True,
        toolset_resolver=toolset_resolver,
        approval_resolver=pool._approval_resolver,
        max_parallel_nodes=pool.config.max_parallel_nodes,
        artifact_storage=await _resolve_default_artifact_storage(pool),
    )
    return _GraphTurnDriver(executor)


async def resolve_llm_model(
    pool: "WorkerPool", agent, override_profile_id: str | None = None,
):
    """Resolve the :class:`ResolvedModel` this turn should run under.

    ``override_profile_id`` wins over the agent's own default when set,
    which is how the session, chat, and graph-node override surfaces
    reach the model layer. Raises :class:`NotFoundError` when the
    resolved profile is absent.
    """
    from primer.model_profile import resolve_model

    return await resolve_model(
        pool._storage,
        default_profile_id=agent.model.profile_id,
        override_profile_id=override_profile_id,
    )


async def resolve_llm_and_model(
    pool: "WorkerPool", agent, override_profile_id: str | None = None,
):
    """Resolve both the adapter and the model facts for one turn.

    Every build path needs the pair. Delegates to
    :func:`primer.model_profile.resolve_llm`, which knows how to build
    an :class:`~primer.llm.aggregated.AggregatedLLM` when the resolved
    profile is ``kind="aggregated"`` -- the naive ``resolve_model`` then
    ``get_llm(resolved.provider_id)`` two-step this replaced would call
    ``get_llm(None)`` for an aggregated profile (see ResolvedModel's
    docstring on why that field is null), which is exactly the loud
    failure that told us this seam needed migrating.
    """
    # Through pool._resolve_llm, not the module function directly: this
    # module's contract is that builders route via the pool so a test
    # monkeypatching pool._resolve_llm still steers resolution (mirrors
    # resolve_llm_model routing through pool._resolve_llm_model above).
    return await pool._resolve_llm(agent, override_profile_id)


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
    if last_reason in ("graph_ended", "graph_failed"):
        return SessionStatus.ENDED
    if last_reason in ("max_tokens", "error", "content_filter"):
        return SessionStatus.WAITING
    return SessionStatus.RUNNING
