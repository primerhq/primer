"""The REST create and the ``create_workspace`` tool are one create path (lead rulings 1 and 2 of ticket 01a16d72).

``POST /v1/workspaces`` refused a Kubernetes provider with ``variant: agent_sandbox`` (501): the variant is accepted on a provider but no backend
builds it. The tool had no such check, so it built a StatefulSet on that provider as if it were ``system``. The refusal now lives in
:meth:`WorkspaceRegistry.materialise`, the one point every create passes (both doors and the bootstrap seed), before any backend is built, and
both doors answer it in the same words: REST 501 ``not_implemented``, the tool ``type=not-implemented``.

The two doors also wrote different first phases: REST ``running`` (``materialise`` returned a live handle), the tool the model's default
``pending``, which the probe promotes at its first good ping. Both now build the row with :func:`new_workspace_row` (the bootstrap seed too), whose
phase is :data:`NEW_WORKSPACE_PHASE`: ``pending`` until the first good probe, which is what the probe is built for. No reader depends on
``running`` at create: no session start, wake or turn reads the workspace's phase, the console colours ``pending`` amber and shows its failure
banner on ``failed`` only, and the one ``running`` check (reopening a ``workspace_lost`` session) concerns a workspace that existed before.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.storage import OffsetPage
from primer.model.workspace import (
    KubernetesTemplateConfig,
    KubernetesWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.model.workspace_refusal import AGENT_SANDBOX_NOT_IMPLEMENTED, WorkspaceNotImplementedError
from primer.toolset.workspaces import build_workspaces_toolset
from tests.api.test_workspaces import _FakeBackend, _provider, _template, app, client, pr, sp  # noqa: F401  (fixtures)


@pytest.fixture
def built() -> list[str]:
    """The provider ids a backend was built for, in order: a refused create builds none."""
    return []


@pytest.fixture
def wsr(sp, built):  # noqa: F811  (overrides the imported module's registry: same fake backend, and it records every build)
    def _factory(provider: WorkspaceProvider) -> _FakeBackend:
        built.append(provider.id)
        return _FakeBackend(provider)

    return WorkspaceRegistry(sp, factory=_factory)


@pytest.fixture
def toolset(sp, wsr):  # noqa: F811
    return build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)


@pytest.fixture
async def agent_sandbox(sp):  # noqa: F811
    """A Kubernetes provider with ``variant: agent_sandbox`` and a template on it, written straight to storage."""
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(
            id="k8s-as",
            provider=WorkspaceProviderType.KUBERNETES,
            config=KubernetesWorkspaceConfig.model_validate(
                {
                    "variant": "agent_sandbox",
                    "connection": {"kind": "in_cluster"},
                    "namespace": "primer",
                    "reachability": {"kind": "in_cluster"},
                }
            ),
        )
    )
    template = WorkspaceTemplate(
        id="tpl-k", description="t", provider_id="k8s-as", backend=KubernetesTemplateConfig(image="primer-runtime:1"),
    )
    await sp.get_storage(WorkspaceTemplate).create(template)
    return template


async def _rows(sp) -> list[Workspace]:  # noqa: F811
    page = await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=50))
    return list(page.items)


# ---- ruling 1: the agent_sandbox refusal ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_refuses_an_agent_sandbox_provider_and_builds_nothing(toolset, agent_sandbox, built, sp) -> None:  # noqa: F811
    result = await toolset.call(tool_name="create_workspace", arguments={"id": "ws-as", "template_id": "tpl-k"})

    assert result.is_error, result.output
    body = json.loads(result.output)
    assert body["type"] == "not-implemented", body
    assert body["message"] == AGENT_SANDBOX_NOT_IMPLEMENTED
    assert built == [], "a refused create built a backend"
    assert await _rows(sp) == [], "a refused create wrote a row"


@pytest.mark.asyncio
async def test_rest_and_the_tool_refuse_it_in_the_same_words(client, toolset, agent_sandbox, built) -> None:  # noqa: F811
    resp = await client.post("/v1/workspaces", json={"id": "ws-as", "template_id": "tpl-k"})
    tool = json.loads((await toolset.call(tool_name="create_workspace", arguments={"id": "ws-as", "template_id": "tpl-k"})).output)

    assert resp.status_code == 501, resp.text
    assert resp.json()["extensions"]["error"] == "not_implemented"
    assert resp.json()["extensions"]["message"] == AGENT_SANDBOX_NOT_IMPLEMENTED == tool["message"]
    assert built == []


@pytest.mark.asyncio
async def test_the_refusal_is_in_the_one_point_every_create_passes(wsr, agent_sandbox, built) -> None:
    """``materialise`` is what REST, the tool and the bootstrap seed all call: the refusal there covers every door, before a backend is built."""
    with pytest.raises(WorkspaceNotImplementedError) as caught:
        await wsr.materialise(template=agent_sandbox, workspace_id="ws-as")

    assert caught.value.message == AGENT_SANDBOX_NOT_IMPLEMENTED
    assert caught.value.provider_id == "k8s-as"
    assert built == []


@pytest.mark.asyncio
async def test_a_system_variant_kubernetes_provider_is_still_built(toolset, sp, built) -> None:  # noqa: F811
    """Only ``agent_sandbox`` is refused: the default ``system`` variant goes on to its backend."""
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(
            id="k8s-sys",
            provider=WorkspaceProviderType.KUBERNETES,
            config=KubernetesWorkspaceConfig.model_validate(
                {"connection": {"kind": "in_cluster"}, "namespace": "primer", "reachability": {"kind": "in_cluster"}}
            ),
        )
    )
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(id="tpl-s", description="t", provider_id="k8s-sys", backend=KubernetesTemplateConfig(image="primer-runtime:1"))
    )

    result = await toolset.call(tool_name="create_workspace", arguments={"id": "ws-sys", "template_id": "tpl-s"})

    assert not result.is_error, result.output
    assert built == ["k8s-sys"]


@pytest.mark.asyncio
async def test_the_tool_says_it_refuses_the_variant(toolset) -> None:
    tools = {tool.id: tool async for tool in toolset.list_tools()}

    description = tools["create_workspace"].description
    assert "``variant: agent_sandbox`` is refused (``type=not-implemented``, REST 501)" in description
    assert "nothing is created" in description.split("agent_sandbox", 1)[1]


# ---- ruling 2: one first phase -------------------------------------------------------------------------------------------------------------


async def _local_template(sp) -> None:  # noqa: F811
    await sp.get_storage(WorkspaceProvider).create(_provider())
    await sp.get_storage(WorkspaceTemplate).create(_template())


@pytest.mark.asyncio
async def test_rest_and_the_tool_store_a_new_workspace_pending(client, toolset, sp) -> None:  # noqa: F811
    await _local_template(sp)

    rest = await client.post("/v1/workspaces", json={"id": "ws-rest", "template_id": "tpl-1"})
    tool = await toolset.call(tool_name="create_workspace", arguments={"id": "ws-tool", "template_id": "tpl-1"})

    assert rest.status_code == 201, rest.text
    assert not tool.is_error, tool.output
    assert rest.json()["phase"] == "pending"
    assert json.loads(tool.output)["phase"] == "pending"
    assert {w.id: w.phase for w in await _rows(sp)} == {"ws-rest": "pending", "ws-tool": "pending"}


@pytest.mark.asyncio
async def test_both_doors_build_the_row_through_the_shared_path(client, toolset, sp, wsr) -> None:  # noqa: F811
    """The one rule is a named one: the shared builder's phase, which every door writes."""
    from primer.api.registries.workspace_registry import NEW_WORKSPACE_PHASE, new_workspace_row

    await _local_template(sp)
    template = await sp.get_storage(WorkspaceTemplate).get("tpl-1")
    row = new_workspace_row(workspace_id="ws-x", template=template, overrides=None, live=await wsr.materialise(template=template, workspace_id="ws-x"))

    assert NEW_WORKSPACE_PHASE == "pending"
    assert row.phase == NEW_WORKSPACE_PHASE
    assert (row.template_snapshot, row.provider_id) == (template, "local-1")


@pytest.mark.asyncio
async def test_the_first_good_probe_makes_a_new_workspace_running(client, toolset, sp, wsr) -> None:  # noqa: F811
    """What ``pending`` waits for: the probe's first good ping promotes the row, whichever door made it."""
    from primer.workspace.probe import WorkspaceProbeTask

    await _local_template(sp)
    assert (await client.post("/v1/workspaces", json={"id": "ws-rest", "template_id": "tpl-1"})).status_code == 201
    assert not (await toolset.call(tool_name="create_workspace", arguments={"id": "ws-tool", "template_id": "tpl-1"})).is_error
    for wid in ("ws-rest", "ws-tool"):
        (await wsr.get_workspace(wid)).ping = AsyncMock(return_value=True)

    await WorkspaceProbeTask(storage_provider=sp, registry=wsr, interval_seconds=0.01, start_delay_seconds=0).tick()

    assert {w.id: w.phase for w in await _rows(sp)} == {"ws-rest": "running", "ws-tool": "running"}
