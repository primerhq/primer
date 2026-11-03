"""The main operator session has its on-disk slot (the bug: a main session made on a sandbox workspace never got one, and its first turn died).

``ensure_main_operator_session`` makes the row with ``create_session`` (row only). The turn needs ``Workspace.get_session(sid)`` to find the slot under
``.state/sessions/<sid>/``, which only ``start_workspace_session`` used to allocate. Now the slot is allocated at creation, and a row that has none (made before
this fix) is given one the first time its turn is built (``ensure_agent_slot``).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.bootstrap.defaults import RESERVED_OPERATOR_AGENT
from primer.bootstrap.prime import ensure_main_operator_session
from primer.model.agent import Agent, AgentModel
from primer.model.principal import PrincipalRef
from primer.model.workspace import ContainerTemplateConfig, WorkspaceRuntimeMeta, WorkspaceTemplate
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace
from primer.workspace.session_factory import SessionFactoryDeps, create_session, ensure_agent_slot
from tests._support.off_golden import open_session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

WS = "prime"
USER = PrincipalRef(type="user", id="user-owner", display="owner", role="admin", source="local")


class Registry:
    def __init__(self, workspace) -> None:
        self.workspace = workspace

    async def get_workspace(self, workspace_id):
        return self.workspace


@pytest.fixture(params=["local", "sandbox"])
async def workspace(request, tmp_path: Path):
    if request.param == "local":
        backend, ws, session = await open_session(tmp_path)
        yield ws
        await session.aclose()
        await backend.aclose()
    else:
        template = WorkspaceTemplate(id="t1", provider_id="c1", description="", backend=ContainerTemplateConfig(image="python:3.13"))
        yield await SandboxWorkspace.materialise(
            workspace_id="ws-k3s", template=template, sandbox=FakeSandbox(root=tmp_path), backend_kind="container",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://fake/ws", token=SecretStr("fake-token")),
        )


async def _operator(sp) -> Agent:
    agent = Agent(id=RESERVED_OPERATOR_AGENT, description="op", model=AgentModel(profile_id="p--m"), tools=["system__list_files"], system_prompt=[])
    await sp.get_storage(Agent).create(agent)
    return agent


async def test_the_main_session_is_made_with_its_on_disk_slot(workspace, fake_storage_provider):
    await _operator(fake_storage_provider)
    deps = SessionFactoryDeps(storage_provider=fake_storage_provider, claim_engine=None, scheduler=None, workspace_registry=Registry(workspace))
    row, created = await ensure_main_operator_session(fake_storage_provider, workspace_id=WS, initiated_by=USER, session_deps=deps)
    assert created
    slot = await workspace.get_session(row.id)
    assert slot is not None, "the turn builds its executor from this slot"
    assert (await slot.info()).name == "main"
    again, created_again = await ensure_main_operator_session(fake_storage_provider, workspace_id=WS, initiated_by=USER, session_deps=deps)
    assert (again.id, created_again) == (row.id, False)


async def test_a_row_made_without_a_slot_is_given_one_when_its_turn_is_built(workspace, fake_storage_provider):
    agent = await _operator(fake_storage_provider)
    row = await create_session(
        workspace_id=WS, binding=AgentSessionBinding(agent_id=RESERVED_OPERATOR_AGENT), initial_instructions=None, graph_input=None, auto_start=False,
        metadata=None, name="main", session_id="sess-prime-main-2", initiated_by=USER,
        deps=SessionFactoryDeps(storage_provider=fake_storage_provider, claim_engine=None, scheduler=None, workspace_registry=None),
    )
    assert await workspace.get_session(row.id) is None, "the install on the owner's cluster: a row and no slot"
    slot = await ensure_agent_slot(workspace, row, agent)
    assert slot is not None and await workspace.get_session(row.id) is not None
    assert await ensure_agent_slot(workspace, row, agent) is await workspace.get_session(row.id), "an existing slot is returned as it is"


def test_the_executor_builder_allocates_a_missing_slot():
    """``build_agent_executor`` is too wide to run here; what matters is that it asks for the slot to be made instead of raising."""
    import inspect

    from primer.worker import executor_builders

    body = inspect.getsource(executor_builders.build_agent_executor)
    assert "ensure_agent_slot(" in body and "On-disk session slot" not in body


async def test_the_first_message_of_a_slotless_row_lands_in_the_history_the_turn_sends(workspace, fake_storage_provider):
    """The owner's k3s: the steer ran while the slot was missing, the instruction reached only the display log, and the model answered 'No user query found'."""
    await _operator(fake_storage_provider)
    global_workspace = workspace

    async def run():
        from primer.session.enqueue import SessionWakeDeps, wake_session

        class Scheduler:
            async def enqueue(self, session_id):
                return None

        class Engine:
            async def upsert(self, *a, **k):
                return None

        row = await create_session(
            workspace_id=WS, binding=AgentSessionBinding(agent_id=RESERVED_OPERATOR_AGENT), initial_instructions=None, graph_input=None, auto_start=False,
            metadata=None, name="main", session_id="sess-prime-main-3", initiated_by=USER,
            deps=SessionFactoryDeps(storage_provider=fake_storage_provider, claim_engine=None, scheduler=None, workspace_registry=None),
        )
        assert await global_workspace.get_session(row.id) is None
        deps = SessionWakeDeps(storage_provider=fake_storage_provider, scheduler=Scheduler(), claim_engine=Engine(), workspace_registry=Registry(global_workspace))
        await wake_session(workspace_id=WS, session_id=row.id, instruction="hello", deps=deps, human_intent=True)
        return row

    row = await run()
    slot = await workspace.get_session(row.id)
    assert slot is not None, "the steer made the slot"
    history = (await slot.log_view()).history
    assert [(m.role, m.parts[0].text) for m in history] == [("user", "hello")], "what the executor sends has the user's message"
    agent = await fake_storage_provider.get_storage(Agent).get(RESERVED_OPERATOR_AGENT)
    assert await ensure_agent_slot(workspace, row, agent) is slot, "building the turn finds that slot and adds nothing"


async def test_a_graph_session_without_a_slot_gets_one_for_its_first_message(workspace, fake_storage_provider):
    from primer.model.workspace_session import GraphSessionBinding
    from primer.workspace.session_factory import ensure_session_slot

    row = await create_session(
        workspace_id=WS, binding=GraphSessionBinding(graph_id="g1"), initial_instructions=None, graph_input=None, auto_start=False, metadata=None,
        session_id="sess-graph-1", initiated_by=USER,
        deps=SessionFactoryDeps(storage_provider=fake_storage_provider, claim_engine=None, scheduler=None, workspace_registry=None),
    )
    assert await workspace.get_session(row.id) is None
    slot = await ensure_session_slot(workspace, row, fake_storage_provider)
    assert slot is not None and await workspace.get_session(row.id) is slot
