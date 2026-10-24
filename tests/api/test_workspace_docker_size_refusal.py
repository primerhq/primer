"""A size the Docker daemon itself refuses is a 422 on ``POST /v1/workspaces`` and on the ``create_workspace`` tool (the G5 audit, G5-07).

Inside the caps of lead ruling 17, Docker still refuses sizes of its own: more CPUs than the host has, memory under its minimum. The raw
``aiodocker.DockerError`` is no ``PrimerError``, so the create used to answer the generic 500 after the rollback, the class the ruling
removed, and the tool's ``except PrimerError`` let it through too. These run the real chain: the real ``DockerRuntimeAdapter`` (over the
small stateful fake daemon of ``tests/workspace/test_docker_create_sandbox_rolls_back.py``) inside the real ``ContainerWorkspaceBackend``,
behind the real router and the real tool. The refusal names the field where the size came from, nothing is left behind (no row, no
volume), and the rollback runs as for any other failure.
"""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import ASGITransport

aiodocker = pytest.importorskip("aiodocker")

from primer.api.app import create_test_app  # noqa: E402
from primer.api.registries import ProviderRegistry, WorkspaceRegistry  # noqa: E402
from primer.model.storage import OffsetPage  # noqa: E402
from primer.model.workspace import (  # noqa: E402
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.toolset.workspaces import build_workspaces_toolset  # noqa: E402
from primer.workspace.container.backend import ContainerWorkspaceBackend  # noqa: E402
from primer.workspace.runtime.docker import DockerRuntimeAdapter  # noqa: E402
from tests._support.caller import caller  # noqa: E402
from tests.api.test_workspaces import _SP  # noqa: E402
from tests.workspace.test_docker_create_sandbox_rolls_back import _FakeDocker  # noqa: E402

_TOO_MANY_CPUS = "Range of CPUs is from 0.01 to 16.00, as there are only 16 CPUs available"
_REFUSED = f"Docker refused this size: {_TOO_MANY_CPUS}."


class _Adapter(DockerRuntimeAdapter):
    """The real adapter, talking to the fake daemon instead of a socket."""

    def __init__(self, config: ContainerWorkspaceConfig, docker: _FakeDocker) -> None:
        super().__init__(config)
        self._fake = docker

    async def initialize(self) -> None:
        self._docker = self._fake

    async def aclose(self) -> None:
        self._docker = None


@pytest.fixture
def daemon() -> _FakeDocker:
    docker = _FakeDocker()

    async def refuse_the_cpus(fake: _FakeDocker, name: str) -> None:
        raise aiodocker.DockerError(400, _TOO_MANY_CPUS)

    docker.create_or_replace_hook = refuse_the_cpus
    return docker


@pytest.fixture
async def sp() -> _SP:
    store = _SP()
    await store.get_storage(WorkspaceProvider).create(WorkspaceProvider(
        id="dock", provider=WorkspaceProviderType.CONTAINER,
        config=ContainerWorkspaceConfig(
            runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
            reachability=ContainerReachabilityHostPort(),
        ),
    ))
    templates = store.get_storage(WorkspaceTemplate)
    await templates.create(WorkspaceTemplate(id="tpl-dock", provider_id="dock", description="docker", backend=ContainerTemplateConfig()))
    await templates.create(WorkspaceTemplate(
        id="tpl-dock-64", provider_id="dock", description="docker, 64 CPUs", backend=ContainerTemplateConfig(cpu_cores=64),
    ))
    return store


@pytest.fixture
def registry(sp, daemon) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=lambda provider: ContainerWorkspaceBackend(provider.config, adapter=_Adapter(provider.config, daemon)))


@pytest.fixture
async def client(sp, registry):
    app = create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=ProviderRegistry(
            sp,  # type: ignore[arg-type]
            llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
            cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
        ),
        workspace_registry=registry,
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/register", json={"username": "dockeradmin", "password": "dockeradminpass1"})
        assert r.status_code == 200, r.text
        yield c


async def _rows(sp) -> list:
    return (await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=10))).items


def _nothing_left(daemon: _FakeDocker) -> bool:
    return daemon.containers_present == set() and daemon.volumes_present == set()


async def test_a_cpu_override_docker_refuses_is_a_422_naming_the_override(client, sp, daemon):
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-dock", "overrides": {"resources": {"cpu_cores": 64}}})

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["type"] == "/errors/validation-error"
    assert body["detail"] == f"overrides.resources.cpu_cores: {_REFUSED}"
    assert body["extensions"]["errors"] == [{
        "loc": ["body", "overrides", "resources", "cpu_cores"], "msg": _REFUSED, "type": "refused_by_provider",
    }]
    assert await _rows(sp) == []
    assert _nothing_left(daemon)
    assert len(daemon.removed) == 1 and daemon.removed[0].startswith("volume:workspace-"), "the rollback removed the volume it made"


async def test_a_template_cpu_docker_refuses_is_a_422_naming_the_template(client, sp, daemon):
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-dock-64"})

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["detail"].startswith(f"backend.cpu_cores: {_REFUSED} Template 'tpl-dock-64' sets backend.cpu_cores: edit the template")
    assert "errors" not in body["extensions"]      # stored in the template, not sent in this request: no `loc`
    assert await _rows(sp) == []
    assert _nothing_left(daemon)


async def test_the_create_workspace_tool_answers_validation_error(sp, registry, daemon):
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=registry)

    result = await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": "tpl-dock", "overrides": {"resources": {"cpu_cores": 64}}},
        ctx=caller("user"),
    )

    assert result.is_error, result.output
    out = json.loads(result.output)
    assert out["type"] == "validation-error"
    assert out["message"] == f"overrides.resources.cpu_cores: {_REFUSED}"
    assert await _rows(sp) == []
    assert _nothing_left(daemon)
