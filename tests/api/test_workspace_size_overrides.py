"""``POST /v1/workspaces`` refuses a size override the workspace's provider cannot honour (platform revamp 2026-10, plan 08
task 4; master Review Focus 5), on the real router.

A Local workspace runs on the server itself and cannot be sized; Docker has no disk quota. The refusal is a 422 whose
``extensions.errors`` names ``overrides.resources.<field>``, and it comes BEFORE the backend is asked to build anything, so
nothing is created, no row and no container. Kubernetes takes all three sizes and Docker takes CPU and memory.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.storage import OffsetPage
from pydantic import SecretStr

from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesTemplateConfig,
    KubernetesWorkspaceConfig,
    LocalWorkspaceConfig,
    ResourceLimits,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from tests.api.test_workspaces import _FakeBackend, _SP, _FakeWorkspace


class _RecordingBackend(_FakeBackend):
    """A backend that remembers what ``create`` was asked for, so a test can tell "refused" from "built then rolled back"."""

    def __init__(self, provider, calls: list) -> None:
        super().__init__(provider)
        self._calls = calls

    async def create(self, template, *, overrides=None, workspace_id=None, resolvers=None):
        self._calls.append({"template": template.id, "overrides": overrides})
        # One backend per provider, each with its own counter: number the workspaces across all of them.
        return await super().create(
            template, overrides=overrides, workspace_id=workspace_id or f"ws-rec-{len(self._calls)}", resolvers=resolvers,
        )


class _Calls(list):
    """What the backends were asked to ``create`` (the list itself), plus ``built``: the provider ids the registry's factory was
    asked to build a backend for. A refused create must reach neither, because every check of ``WorkspaceRegistry.materialise``
    runs before ``get_backend`` (review of #789, nit 1): ``create`` alone would stay empty even if a check moved after the backend
    had been built (a Docker daemon connected to, a cluster client opened)."""

    def __init__(self) -> None:
        super().__init__()
        self.built: list[str] = []


def _assert_nothing_built(calls: _Calls) -> None:
    assert (list(calls), calls.built) == ([], []), "a refused create reached the backend factory or create()"


@pytest.fixture
def backend_calls() -> _Calls:
    return _Calls()


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def app(sp, backend_calls):
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )
    def factory(provider):
        backend_calls.built.append(provider.id)
        return _RecordingBackend(provider, backend_calls)

    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=registry,
        workspace_registry=WorkspaceRegistry(sp, factory=factory),
    )


@pytest.fixture
async def client(app, sp):
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/register", json={"username": "sizeadmin", "password": "sizeadminpass1"})
        assert r.status_code == 200, r.text
        providers = sp.get_storage(WorkspaceProvider)
        templates = sp.get_storage(WorkspaceTemplate)
        await providers.create(WorkspaceProvider(
            id="loc", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-size-tests"),
        ))
        await providers.create(WorkspaceProvider(
            id="dock", provider=WorkspaceProviderType.CONTAINER,
            config=ContainerWorkspaceConfig(
                runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
                reachability=ContainerReachabilityHostPort(),
            ),
        ))
        await providers.create(WorkspaceProvider(
            id="k3s", provider=WorkspaceProviderType.KUBERNETES,
            config=KubernetesWorkspaceConfig(
                connection=K8sConnectionInCluster(), namespace="ns", reachability=K8sReachabilityInCluster(),
            ),
        ))
        await templates.create(WorkspaceTemplate(id="tpl-loc", provider_id="loc", description="local"))
        await templates.create(WorkspaceTemplate(
            id="tpl-dock", provider_id="dock", description="docker", backend=ContainerTemplateConfig(),
        ))
        await templates.create(WorkspaceTemplate(
            id="tpl-k3s", provider_id="k3s", description="kubernetes", backend=KubernetesTemplateConfig(image="rt:1"),
        ))
        yield c


async def _workspace_rows(sp) -> list:
    return (await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=10))).items


def _create(client, template_id: str, resources: dict):
    return client.post("/v1/workspaces", json={"template_id": template_id, "overrides": {"resources": resources}})


async def test_docker_disk_override_is_a_422_naming_the_field(client, backend_calls, sp):
    r = await _create(client, "tpl-dock", {"disk_bytes": 10737418240})

    assert r.status_code == 422
    body = r.json()
    assert body["type"] == "/errors/validation-error"
    assert body["extensions"]["errors"] == [{
        "loc": ["body", "overrides", "resources", "disk_bytes"],
        "msg": "Docker cannot limit a workspace's disk. Remove this override.",
        "type": "unsupported_by_provider",
    }]
    assert body["detail"] == "overrides.resources.disk_bytes: Docker cannot limit a workspace's disk. Remove this override."
    _assert_nothing_built(backend_calls)
    assert await _workspace_rows(sp) == []


async def test_local_size_override_is_a_422(client, backend_calls, sp):
    r = await _create(client, "tpl-loc", {"cpu_cores": 2})

    assert r.status_code == 422
    assert r.json()["extensions"]["errors"] == [{
        "loc": ["body", "overrides", "resources", "cpu_cores"],
        "msg": "A Local workspace runs on the server itself and cannot be sized. Remove this override.",
        "type": "unsupported_by_provider",
    }]
    _assert_nothing_built(backend_calls)
    assert await _workspace_rows(sp) == []


async def test_local_names_every_size_field_it_was_given(client, backend_calls):
    r = await _create(client, "tpl-loc", {"cpu_cores": 1, "memory_bytes": 2**30, "disk_bytes": 2**30})

    assert r.status_code == 422
    assert [e["loc"][-1] for e in r.json()["extensions"]["errors"]] == ["cpu_cores", "memory_bytes", "disk_bytes"]
    _assert_nothing_built(backend_calls)


async def test_docker_cpu_and_memory_reach_the_backend(client, backend_calls):
    r = await _create(client, "tpl-dock", {"cpu_cores": 1.5, "memory_bytes": 2147483648})

    assert r.status_code == 201, r.text
    assert len(backend_calls) == 1 and backend_calls.built == ["dock"]
    sent = backend_calls[0]["overrides"].resources
    assert sent.cpu_cores == 1.5 and sent.memory_bytes == 2147483648 and sent.disk_bytes is None


async def test_kubernetes_size_override_is_kept_on_the_row(client, backend_calls, sp):
    r = await _create(client, "tpl-k3s", {"cpu_cores": 4, "memory_bytes": 8 * 2**30, "disk_bytes": 21474836480})

    assert r.status_code == 201, r.text
    assert r.json()["overrides"]["resources"]["disk_bytes"] == 21474836480
    [row] = await _workspace_rows(sp)
    assert row.overrides.resources.disk_bytes == 21474836480 and row.overrides.resources.cpu_cores == 4
    assert backend_calls[0]["overrides"].resources.disk_bytes == 21474836480


async def test_a_zero_size_is_refused_by_the_model(client, backend_calls):
    r = await _create(client, "tpl-k3s", {"cpu_cores": 0})

    assert r.status_code == 422
    assert r.json()["extensions"]["errors"][0]["loc"][-1] == "cpu_cores"
    _assert_nothing_built(backend_calls)


async def test_no_size_is_asked_of_any_provider_when_the_override_has_none(client, backend_calls):
    for template_id in ("tpl-loc", "tpl-dock", "tpl-k3s"):
        r = await client.post("/v1/workspaces", json={"template_id": template_id})
        assert r.status_code == 201, (template_id, r.text)
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-loc", "overrides": {"env": {"A": "b"}, "resources": {}}})
    assert r.status_code == 201, r.text
    assert len(backend_calls) == 4 and backend_calls.built == ["loc", "dock", "k3s"]      # one backend per provider, built on first use


async def test_the_refusal_comes_before_the_id_is_taken(client, backend_calls, sp):
    """A refused create leaves the id free: the same request without the size creates the workspace."""
    refused = await client.post("/v1/workspaces", json={
        "id": "ws-sized", "template_id": "tpl-loc", "overrides": {"resources": {"memory_bytes": 2**30}},
    })
    assert refused.status_code == 422 and await _workspace_rows(sp) == []
    _assert_nothing_built(backend_calls)

    again = await client.post("/v1/workspaces", json={"id": "ws-sized", "template_id": "tpl-loc"})
    assert again.status_code == 201, again.text


async def test_an_accepted_create_builds_a_backend_so_the_nothing_built_assertions_can_fail(client, backend_calls):
    """The control for ``_assert_nothing_built`` in every refusal test: the recorder does see the backend a create builds."""
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-k3s"})

    assert r.status_code == 201, r.text
    assert backend_calls.built == ["k3s"] and len(backend_calls) == 1


def test_the_422_is_documented_on_the_create_route(app):
    assert "422" in app.openapi()["paths"]["/v1/workspaces"]["post"]["responses"]


def test_the_fake_workspace_the_tests_build_is_the_one_the_other_api_tests_use():
    """Guards the import above: these tests lean on ``tests.api.test_workspaces``'s fakes for the 201 path."""
    assert _FakeWorkspace("ws-x").id == "ws-x"


# ---- lead ruling 17 of plan 08 (from the review of #782): absurd sizes and bad env names are a 422, not a 500 after a rollback ----


@pytest.mark.parametrize("field, absurd", [("cpu_cores", 1e308), ("memory_bytes", 10**30), ("disk_bytes", 10**30)])
async def test_an_absurd_size_override_is_a_422_with_the_field_in_loc(client, backend_calls, sp, field, absurd):
    r = await _create(client, "tpl-k3s", {field: absurd})

    assert r.status_code == 422, r.text
    errors = r.json()["extensions"]["errors"]
    assert [e["loc"] for e in errors] == [["body", "overrides", "resources", field]], errors
    _assert_nothing_built(backend_calls)
    assert await _workspace_rows(sp) == []


async def test_the_caps_themselves_are_accepted(client, backend_calls):
    r = await _create(client, "tpl-k3s", {"cpu_cores": 1024, "memory_bytes": 64 * 2**40, "disk_bytes": 64 * 2**40})

    assert r.status_code == 201, r.text
    assert len(backend_calls) == 1


async def test_a_gpu_count_above_the_cap_is_a_422_on_the_template_write(client, sp):
    body = {"id": "tpl-gpu", "provider_id": "k3s", "description": "d", "backend": {"kind": "kubernetes", "image": "rt:1", "gpu": {"count": 65}}}

    r = await client.post("/v1/workspace_templates", json=body)

    assert r.status_code == 422, r.text
    assert any(e["loc"][-2:] == ["gpu", "count"] for e in r.json()["extensions"]["errors"]), r.text
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-gpu") is None


async def test_a_template_with_an_absurd_cpu_is_a_422_that_names_the_template_and_the_field(client, backend_calls, sp):
    """Seeded through storage, as a row written before the cap existed: the template cannot be refused at load, only at create."""
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="tpl-big", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        resources=ResourceLimits(cpu_cores=1e308),
    ))

    r = await client.post("/v1/workspaces", json={"template_id": "tpl-big"})

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["type"] == "/errors/validation-error"
    assert "template 'tpl-big'" in body["detail"] and "resources.cpu_cores" in body["detail"], body
    assert "errors" not in body["extensions"], body
    _assert_nothing_built(backend_calls)
    assert await _workspace_rows(sp) == []

    ok = await client.post("/v1/workspaces", json={"template_id": "tpl-big", "overrides": {"resources": {"cpu_cores": 2}}})
    assert ok.status_code == 201, ok.text     # the override replaces the absurd field


#: What an ``invalid_env_name`` refusal says (lead ruling 17 of plan 08, FINAL from the review of #789).
_INVALID_ENV_NAME = "An environment variable name is 1 to 256 characters of ASCII letters, digits, '_', '-' and '.', and does not start with a digit. Rename or remove it."

#: Names Kubernetes accepts that the first (POSIX-only) version of the rule refused, one that starts with an underscore, and the
#: longest allowed.
_KUBERNETES_NAMES = ["my-var", "a.b", "_x", "a" * 256]


@pytest.mark.parametrize("name", ["1BAD", "a b", "A=B", "", "café", "9"])
async def test_an_invalid_env_name_in_the_overrides_is_a_422_naming_it(client, backend_calls, sp, name):
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-loc", "overrides": {"env": {name: "v"}}})

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["type"] == "/errors/validation-error"
    assert body["extensions"]["errors"] == [{
        "loc": ["body", "overrides", "env", name], "msg": _INVALID_ENV_NAME, "type": "invalid_env_name",
    }]
    assert body["detail"] == f"overrides.env.{ascii(name)}: {_INVALID_ENV_NAME}"
    _assert_nothing_built(backend_calls)
    assert await _workspace_rows(sp) == []


async def test_a_257_character_env_name_is_a_422_and_the_message_cuts_it(client, backend_calls, sp):
    name = "a" * 257

    r = await client.post("/v1/workspaces", json={"template_id": "tpl-k3s", "overrides": {"env": {name: "v"}}})

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["extensions"]["errors"] == [{
        "loc": ["body", "overrides", "env", name], "msg": _INVALID_ENV_NAME, "type": "invalid_env_name",
    }]
    assert name not in body["detail"] and "..." in body["detail"]
    _assert_nothing_built(backend_calls)
    assert await _workspace_rows(sp) == []


async def test_a_name_with_a_newline_is_shown_escaped_in_the_message(client, backend_calls):
    """The message is logged; the name is the caller's own input, so it goes through ``ascii()`` (review of #789, nit 2)."""
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-loc", "overrides": {"env": {"a\nb": "v"}}})

    assert r.status_code == 422, r.text
    body = r.json()
    assert "\n" not in body["detail"] and "'a\\nb'" in body["detail"], body["detail"]
    assert body["extensions"]["errors"][0]["loc"] == ["body", "overrides", "env", "a\nb"]
    _assert_nothing_built(backend_calls)


@pytest.mark.parametrize("template_id", ["tpl-loc", "tpl-dock", "tpl-k3s"])
@pytest.mark.parametrize("name", ["PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"])
async def test_a_runtime_token_name_is_a_422_that_names_it_and_never_shows_the_value(client, backend_calls, template_id, name):
    r = await client.post("/v1/workspaces", json={"template_id": template_id, "overrides": {"env": {name: "hunter2-the-value"}}})

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["extensions"]["errors"][0]["loc"] == ["body", "overrides", "env", name]
    assert body["extensions"]["errors"][0]["type"] == "reserved_env_name"
    assert name in body["detail"] and "hunter2" not in r.text
    _assert_nothing_built(backend_calls)


@pytest.mark.parametrize("name, kind", [
    ("has space", "invalid_env_name"), ("1BAD", "invalid_env_name"), ("A=B", "invalid_env_name"), ("a" * 257, "invalid_env_name"),
    ("PRIMER_RUNTIME_TOKEN", "reserved_env_name"), ("RUNTIME_TOKEN", "reserved_env_name"),
])
async def test_a_template_env_the_cluster_would_refuse_is_a_422_at_create(client, backend_calls, sp, name, kind):
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="tpl-env", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        env={name: SecretStr("the-secret-value")},
    ))

    r = await client.post("/v1/workspaces", json={"template_id": "tpl-env"})

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["type"] == "/errors/validation-error"
    assert body["detail"].startswith("template 'tpl-env' sets env.") and body["detail"].endswith(
        _INVALID_ENV_NAME if kind == "invalid_env_name" else "This name is reserved for the workspace runtime's token and cannot be set. Remove it."
    )
    if len(ascii(name)) <= 80:
        assert f"env.{ascii(name)}:" in body["detail"]
    assert "errors" not in body["extensions"]      # the name is stored in the template, not sent in this request: no `loc`
    assert "the-secret-value" not in r.text
    _assert_nothing_built(backend_calls)
    assert await _workspace_rows(sp) == []


@pytest.mark.parametrize("template_id", ["tpl-loc", "tpl-dock", "tpl-k3s"])
@pytest.mark.parametrize("name", _KUBERNETES_NAMES)
async def test_a_kubernetes_env_name_in_the_overrides_creates_the_workspace(client, backend_calls, template_id, name):
    r = await client.post("/v1/workspaces", json={"template_id": template_id, "overrides": {"env": {name: "v"}}})

    assert r.status_code == 201, r.text
    assert len(backend_calls) == 1 and list(backend_calls[0]["overrides"].env) == [name]


@pytest.mark.parametrize("name", _KUBERNETES_NAMES)
async def test_a_stored_template_with_a_kubernetes_env_name_still_creates_workspaces(client, backend_calls, sp, name):
    """The first (POSIX-only) version of the rule turned such a template into a 422 on every create, the boot's default workspace
    included (its 422 is swallowed by the ensure pass, so that workspace was never made)."""
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="tpl-env", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        env={name: SecretStr("v")},
    ))

    r = await client.post("/v1/workspaces", json={"template_id": "tpl-env"})

    assert r.status_code == 201, r.text
    assert len(backend_calls) == 1 and backend_calls[0]["template"] == "tpl-env"


async def test_good_env_names_still_create_the_workspace(client, backend_calls):
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-k3s", "overrides": {"env": {"_OK1": "v", "PATH_EXTRA": "x"}}})

    assert r.status_code == 201, r.text
    assert len(backend_calls) == 1
