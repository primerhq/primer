"""Task 6, end-to-end: attribution survives the worker resume boundary.

Spec §8.2: a session's ``initiated_by`` is persisted at creation time and
re-hydrated into ``ctx.identity`` whenever the worker (re-)builds the
executor for that session -- including on resume, when the row is
re-``get``-fetched from storage rather than reusing the in-memory object
the create call handed back. A trigger-fired (or created-without-invoke)
session must stay attributed to the ORIGINATING principal -- never
silently promoted to a live/stale identity -- and a historical row with no
``initiated_by`` falls back to the reserved system principal (§13).

Mirrors the fixtures in
``tests/workspace/test_session_factory_initiated_by.py`` (persist via the
real ``create_session`` factory) and
``tests/worker/test_pool.py::test_build_agent_executor_returns_turn_driver``
(drive the worker's executor-build path with fake provider/workspace
stubs).
"""

from __future__ import annotations

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.model.agent import Agent, AgentModel
from primer.model.principal import PrincipalRef
from primer.model.provider import LLMModel
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import AgentSessionBinding, WorkspaceSession
from primer.worker.pool import WorkerPool
from primer.workspace.session_factory import SessionFactoryDeps, create_session

from tests.conftest import _FakeStorageProvider


class _FakeScheduler:
    async def enqueue(self, sid: str) -> None:
        pass


class _FakeClaimEngineForFactory:
    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        pass


class _FakeAgentSessionForBuild:
    """Just enough on-disk AgentSession surface for the executor's
    composite-prompt builder + ``ToolExecutionManager.for_workspace``."""

    def __init__(self, sid: str, workspace_id: str) -> None:
        self.session_id = sid
        self.workspace_id = workspace_id
        self.workspace_tools: list = []
        self.system_prompt_fragment = "[fake workspace prompt]"


class _FakeWorkspaceForBuild:
    def __init__(self, session_stub: _FakeAgentSessionForBuild) -> None:
        self.id = session_stub.workspace_id
        self._session = session_stub

    async def get_session(self, session_id):
        if session_id != self._session.session_id:
            return None
        return self._session


def _build_pool(storage_provider: _FakeStorageProvider) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,  # type: ignore[arg-type]
        storage=storage_provider,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={}),
    )

    fake_llm = object()
    fake_llm_model = LLMModel(name="m-1", context_length=8000)

    async def _get_llm(provider_id):
        return fake_llm

    async def _get_toolset(_id):
        raise AssertionError("this test's agent registers no toolsets")

    async def _resolve_llm_model(_agent):
        return fake_llm_model

    pool._provider_registry = type(
        "R",
        (),
        {
            "get_llm": staticmethod(_get_llm),
            "get_toolset": staticmethod(_get_toolset),
        },
    )()
    pool._resolve_llm_model = _resolve_llm_model
    return pool


async def _persist_and_refetch(
    fake_storage_provider: _FakeStorageProvider,
    *,
    initiated_by: PrincipalRef | None,
) -> WorkspaceSession:
    """Persist a session row via the REAL ``create_session`` factory (the
    same path the REST router / trigger dispatcher use), then re-``get`` it
    off storage -- mirroring the worker's resume-side read
    (``pool._load_session``) instead of reusing the in-memory row the
    factory call returned."""
    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id="ag-1",
            description="test agent",
            model=AgentModel(provider_id="prov-1", model_name="m-1"),
            tools=[],
            system_prompt=["sys"],
        )
    )
    deps = SessionFactoryDeps(
        storage_provider=fake_storage_provider,
        claim_engine=_FakeClaimEngineForFactory(),
        scheduler=_FakeScheduler(),
        workspace_registry=None,
    )
    created = await create_session(
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        initial_instructions=None,
        graph_input=None,
        auto_start=False,
        metadata={},
        deps=deps,
        initiated_by=initiated_by,
    )
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    refetched = await storage.get(created.id)
    assert refetched is not None
    return refetched


@pytest.mark.asyncio
async def test_resume_reconstructs_trigger_identity_not_worker(
    fake_storage_provider: _FakeStorageProvider,
) -> None:
    """A trigger-fired session, rebuilt on the worker/resume side, still
    carries the ORIGINATING trigger identity -- a trigger stays a trigger,
    never impersonates a user or the resuming worker's own principal."""
    ref = PrincipalRef(
        type="trigger", id="t-1", display="nightly", role=None, source="internal",
    )
    session = await _persist_and_refetch(fake_storage_provider, initiated_by=ref)
    assert session.initiated_by is not None
    assert session.initiated_by.type == "trigger"  # sanity: row round-tripped

    workspace = _FakeWorkspaceForBuild(
        _FakeAgentSessionForBuild(session.id, session.workspace_id)
    )
    pool = _build_pool(fake_storage_provider)

    driver = await pool._build_executor(session, workspace)

    identity = driver._executor._execution_context.identity
    assert identity is not None
    assert identity.type == "trigger"
    assert identity.id == "t-1"


@pytest.mark.asyncio
async def test_resume_historical_row_without_initiated_by_falls_back_to_system(
    fake_storage_provider: _FakeStorageProvider,
) -> None:
    """A historical row created before attribution landed (``initiated_by``
    is ``None``) resolves to the reserved system principal on resume, per
    §13 -- never left ``None`` and never silently promoted to a live
    identity."""
    session = await _persist_and_refetch(fake_storage_provider, initiated_by=None)
    assert session.initiated_by is None  # sanity: historical row shape

    workspace = _FakeWorkspaceForBuild(
        _FakeAgentSessionForBuild(session.id, session.workspace_id)
    )
    pool = _build_pool(fake_storage_provider)

    driver = await pool._build_executor(session, workspace)

    identity = driver._executor._execution_context.identity
    assert identity == PrincipalRef.system()
