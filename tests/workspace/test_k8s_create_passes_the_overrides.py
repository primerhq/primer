"""``KubernetesWorkspaceBackend.create`` hands the per-workspace overrides to the manifest builder (platform revamp 2026-10,
plan 08 task 2).

``_build`` used to build the StatefulSet from the template alone: an override's ``env`` reached only the init commands, and the
manifest had no idea of a per-workspace size. The pod wait is stubbed to fail right after the StatefulSet is created, so the
body the cluster was given is the thing under test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from primer.model.except_ import ConfigError
from primer.model.workspace import WorkspaceResourceOverrides, WorkspaceTemplateOverrides
from tests.workspace.test_k8s_client_is_closed_when_the_build_does_not_finish import TEMPLATE, _backend


async def _statefulset_body(overrides: WorkspaceTemplateOverrides | None) -> dict:
    backend = _backend()
    backend._wait_for_pod_running = AsyncMock(side_effect=ConfigError("stop here"))
    with pytest.raises(ConfigError, match="stop here"):
        await backend.create(TEMPLATE, overrides=overrides, workspace_id="ws-1")
    backend._apps_v1.create_namespaced_stateful_set.assert_awaited_once()
    return backend._apps_v1.create_namespaced_stateful_set.await_args.kwargs["body"]


async def test_create_builds_the_manifest_with_the_overrides():
    body = await _statefulset_body(WorkspaceTemplateOverrides(
        env={"X": "y"}, resources=WorkspaceResourceOverrides(disk_bytes=5 * 2**30),
    ))

    container = body["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "X", "value": "y"} in container["env"]
    assert body["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "5Gi"


async def test_create_passes_a_cpu_and_memory_override_to_the_pod():
    body = await _statefulset_body(WorkspaceTemplateOverrides(
        resources=WorkspaceResourceOverrides(cpu_cores=0.5, memory_bytes=256 * 2**20),
    ))

    resources = body["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert resources["limits"] == {"cpu": "0.5", "memory": str(256 * 2**20)} == resources["requests"]


@pytest.mark.parametrize("overrides", [None, WorkspaceTemplateOverrides()], ids=["none", "empty"])
async def test_create_without_overrides_builds_the_template_as_it_is(overrides):
    body = await _statefulset_body(overrides)

    container = body["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [] and "resources" not in container
    assert body["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "10Gi"
