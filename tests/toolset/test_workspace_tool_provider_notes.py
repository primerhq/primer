"""The workspace and template tools say which field applies to which provider (platform revamp 2026-10, plan 08 task 8, design 14.4).

An agent that writes a template or creates a workspace reads the tool description and nothing else, so the description has to say what the
server will do with each field: which provider takes a size (Kubernetes all three, Docker CPU and memory, Local none), where CPU and memory
live per backend, which placement fields exist and which of them only an admin may set (lead ruling 15: the GPU resource name beyond
``nvidia.com/gpu``), what an environment variable name may be (ruling 17, final: Kubernetes' own rule and 256 characters), the sane maxima, and
what a Docker image has to contain (and the 503 when it does not).

The numbers and names in the descriptions are the CODE'S: each assertion below derives its expected text from the constant the server enforces
(``MAX_WORKSPACE_CPU_CORES``, ``ENV_NAME_PATTERN``, ...), so a limit that moves makes this fail until the description follows it.
"""

from __future__ import annotations

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.principal import PrincipalRef
from primer.model.workspace import (
    DEFAULT_GPU_RESOURCE_NAME,
    DOCKER_RUNTIME_IMAGE,
    MAX_GPU_COUNT,
    MAX_WORKSPACE_CPU_CORES,
    MAX_WORKSPACE_DISK_BYTES,
    MAX_WORKSPACE_MEMORY_BYTES,
    WorkspaceTemplate,
)
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import build_workspaces_toolset
from primer.workspace.env_names import ENV_NAME_PATTERN, MAX_ENV_NAME_LENGTH, RUNTIME_TOKEN_ENV_NAMES
from tests.toolset._desc_conformance import assert_tool_conforms
from tests.toolset.test_workspaces import _SP, _StubBackend

_TIB = 2**40
_TEMPLATE_TOOLS = ["create_workspace_template", "update_workspace_template"]


@pytest.fixture
async def world():
    sp = _SP()
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp, factory=_StubBackend))
    return toolset, {tool.id: tool async for tool in toolset.list_tools()}


@pytest.fixture
async def tools(world):
    return world[1]


def _kubernetes_example(tool):
    examples = [ex for ex in tool.examples if ex.args["entity"]["backend"].get("kind") == "kubernetes"]
    assert len(examples) == 1, (tool.id, [ex.args for ex in tool.examples])
    return examples[0]


def test_create_workspace_says_which_provider_takes_which_size_field(tools) -> None:
    d = tools["create_workspace"].description

    for word in ("overrides.resources", "cpu_cores", "memory_bytes", "disk_bytes", "Local", "Docker", "Kubernetes", "validation-error"):
        assert word in d, word


def test_create_workspace_gives_each_providers_rule_in_one_place(tools) -> None:
    """Kubernetes takes all three sizes; Docker refuses disk (it has no quota); Local takes none, and the refusal names the field."""
    d = tools["create_workspace"].description

    assert "``cpu_cores`` and ``memory_bytes`` on Kubernetes and Docker" in d
    assert "``disk_bytes`` on Kubernetes only" in d
    assert "Local" in d and "takes none" in d
    assert "Docker refuses ``disk_bytes``" in d
    assert "overrides.resources.<field>" in d and "nothing is created" in d


def test_create_workspace_carries_the_caps(tools) -> None:
    d = tools["create_workspace"].description

    assert f"{MAX_WORKSPACE_CPU_CORES} CPU cores" in d
    assert f"{MAX_WORKSPACE_MEMORY_BYTES // _TIB} TiB of memory" in d
    assert f"{MAX_WORKSPACE_DISK_BYTES // _TIB} TiB of disk" in d


def test_create_workspace_carries_the_environment_name_rule(tools) -> None:
    """Lead ruling 17, final: Kubernetes' own env-name rule, at most 256 characters, the runtime token names reserved."""
    d = tools["create_workspace"].description

    assert ENV_NAME_PATTERN in d
    assert f"at most {MAX_ENV_NAME_LENGTH} characters" in d
    assert "``..``" in d and "``.``" in d
    for name in sorted(RUNTIME_TOKEN_ENV_NAMES):
        assert name in d, name
    assert "reaches the pod" in d, "on Kubernetes the override env reaches the pod itself"


def test_create_workspace_says_what_a_docker_image_must_contain_and_the_503(tools) -> None:
    d = tools["create_workspace"].description

    assert "backend.image" in d and DOCKER_RUNTIME_IMAGE in d
    assert "30 seconds" in d and "503" in d and "names the image" in d
    assert f"FROM ``{DOCKER_RUNTIME_IMAGE}``" in d


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_template_tools_name_the_placement_fields_and_the_admin_only_ones(tools, name) -> None:
    d = tools[name].description

    for word in (
        "node_selector", "tolerations", "gpu", "service_account_name", "image_pull_policy", "admin-only",
        "resources.cpu_cores", "backend.cpu_cores", "pvc_size", DOCKER_RUNTIME_IMAGE,
    ):
        assert word in d, word


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_template_tools_carry_the_gpu_rule(tools, name) -> None:
    """Ruling 15: only the default resource name is user tier; any other is admin-only; kubernetes.io/ and requests. are refused for all."""
    d = tools[name].description

    assert "``gpu``" in d and "{count, resource_name}" in d
    assert DEFAULT_GPU_RESOURCE_NAME in d
    assert f"a ``gpu.resource_name`` other than ``{DEFAULT_GPU_RESOURCE_NAME}``" in d
    assert "kubernetes.io/" in d and "requests." in d and "refused for everyone" in d
    assert f"1 to {MAX_GPU_COUNT}" in d


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_template_tools_name_the_user_tier_placement_fields_and_the_admin_only_ones_apart(tools, name) -> None:
    d = tools[name].description

    user_tier = "``node_selector``, a ``gpu`` count with the default resource name and ``image_pull_policy`` are open to every user"
    assert user_tier in d
    admin_only = d.split("The admin-only fields", 1)[1].split(user_tier, 1)[0]
    for field in ("tolerations", "service_account_name", "gpu.resource_name", "extra_mounts", "pod_overrides", "secret"):
        assert field in admin_only, field
    for field in ("node_selector", "image_pull_policy"):
        assert field not in admin_only, f"{field} is user tier"


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_template_tools_say_where_cpu_memory_and_disk_live_per_backend(tools, name) -> None:
    d = tools[name].description

    assert "``resources.cpu_cores`` and ``resources.memory_bytes``" in d      # Kubernetes
    assert "``backend.cpu_cores`` and ``backend.memory_bytes``" in d          # Docker
    assert "``backend.pvc_size``" in d                                        # Kubernetes disk
    assert "no disk setting" in d                                             # Docker
    assert "cpu_request" in d and "ignored" in d, "the old request/limit strings are named so nobody sets them"


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_template_tools_carry_the_runtime_requirement_the_503_and_the_caps(tools, name) -> None:
    d = tools[name].description

    assert f"FROM ``{DOCKER_RUNTIME_IMAGE}``" in d
    assert "30 seconds" in d and "503" in d and "names the image" in d
    assert f"{MAX_WORKSPACE_CPU_CORES} cores" in d and f"{MAX_WORKSPACE_MEMORY_BYTES // _TIB} TiB" in d
    assert ENV_NAME_PATTERN in d, "a template env name follows the same rule as overrides.env"


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_template_tools_tell_a_kubernetes_author_to_set_the_entrypoint(tools, name) -> None:
    """With no ``backend.entrypoint`` the pod runs ``sleep infinity`` and the runtime never starts (plan 08 ruling 7: the default stays a ticket)."""
    d = tools[name].description

    assert "backend.entrypoint" in d and '["python", "-m", "primer_runtime.server"]' in d
    assert "sleep infinity" in d and "the runtime never starts" in d


@pytest.mark.parametrize("name", _TEMPLATE_TOOLS)
def test_template_tools_point_a_non_admin_at_the_provider_choices(tools, name) -> None:
    assert "list_workspace_provider_choices" in tools[name].description


def test_update_workspace_template_keeps_its_note_about_masked_secrets(tools) -> None:
    d = tools["update_workspace_template"].description

    assert "returned masked" in d and "send the body you read back" in d


def test_the_kubernetes_template_example_is_a_working_one(tools) -> None:
    """One Kubernetes example with node_selector, gpu and image_pull_policy on each template tool, and it validates as a template."""
    from primer.workspace.template_privilege import admin_only_template_changes

    for name in _TEMPLATE_TOOLS:
        entity = _kubernetes_example(tools[name]).args["entity"]
        backend = entity["backend"]
        assert backend["node_selector"] and backend["gpu"] and backend["image_pull_policy"]
        assert backend["gpu"]["count"] >= 1 and "resource_name" not in backend["gpu"], "the example uses the default GPU resource name"
        assert backend["entrypoint"] == ["python", "-m", "primer_runtime.server"]
        row = WorkspaceTemplate.model_validate(entity)          # the example is a template the server would accept
        assert row.backend.kind == "kubernetes"
        # ... and one a role=user caller may write: nothing in it is admin-only.
        assert admin_only_template_changes(row, None) == []


async def test_a_role_user_run_gets_both_kubernetes_examples_accepted(world) -> None:
    """The examples are run as written, by a user (not an admin): create first, then the update of the same row."""
    toolset, tools = world
    ctx = ToolContext(
        tool_call_id="call-1", session_id="sess-1", workspace_id=None,
        initiated_by=PrincipalRef(type="user", id="user-1", display="user", role="user", source="local"),
    )

    for name in _TEMPLATE_TOOLS:
        result = await toolset.call(tool_name=name, arguments=_kubernetes_example(tools[name]).args, ctx=ctx)

        assert not result.is_error, (name, result.output)


def test_the_create_workspace_example_sizes_a_kubernetes_workspace(tools) -> None:
    sized = [ex for ex in tools["create_workspace"].examples if "resources" in (ex.args.get("overrides") or {})]
    assert len(sized) == 1
    assert sized[0].args == {
        "template_id": "k8s-python",
        "overrides": {"resources": {"cpu_cores": 4, "memory_bytes": 8589934592, "disk_bytes": 21474836480}},
    }


@pytest.mark.parametrize("name", ["create_workspace", "create_workspace_template", "update_workspace_template"])
def test_the_examples_validate(tools, name) -> None:
    assert_tool_conforms(tools[name])


def test_the_docker_wait_the_descriptions_give_is_the_adapters() -> None:
    """The descriptions say 30 seconds; the Docker adapter waits ``_READY_TIMEOUT_S``. Read from the source: the adapter imports ``aiodocker``,
    an optional extra, and this must hold on an install without it too."""
    import re
    from pathlib import Path

    import primer.workspace

    source = (Path(primer.workspace.__file__).parent / "runtime" / "docker.py").read_text(encoding="utf-8")
    match = re.search(r"^_READY_TIMEOUT_S = ([0-9.]+)$", source, re.MULTILINE)
    assert match is not None and float(match.group(1)) == 30.0
