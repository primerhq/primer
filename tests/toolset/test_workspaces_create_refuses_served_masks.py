"""``create_workspace_provider`` and ``create_workspace_template`` refuse a body that carries a mask a ``get_*`` served (ticket 01a1212a, round 1 of #711, nit N2).

The workspaces toolset has its own create handler (``_make_create_handler``) next to the generic one in ``_system_crud``; only the generic one refused a served mask, so the copy-a-row move
(``get_workspace_provider``, a new id, ``create_workspace_provider``) stored ``**********`` as the Kubernetes service-account token, and a template's ``env`` mask as the variable's value.
A refusal is ``type=validation-error`` and nothing is stored; a real secret is stored as it always was. (``create_python_toolset`` takes only a source and a timeout, so it has no secret to refuse.)
"""

from __future__ import annotations

import json

import pytest

from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate
from tests._support.caller import ADMIN_CALLER
from tests.toolset.test_workspaces import sp, toolset, workspace_registry  # noqa: F401  (fixtures)


def _k8s(token: str, provider_id: str = "wp-copy") -> dict:
    return {
        "id": provider_id, "provider": "kubernetes",
        "config": {
            "kind": "kubernetes",
            "connection": {"kind": "service_account_token", "apiserver_url": "https://k8s.home.example:6443", "ca_data": "ca", "token": token},
            "namespace": "ns", "reachability": {"kind": "in_cluster"},
        },
    }


def _template(env: dict, template_id: str = "tpl-copy") -> dict:
    return {"id": template_id, "description": "dev", "provider_id": "local-1", "env": env}


@pytest.mark.asyncio
@pytest.mark.parametrize("served", ["**********", "**********abcd"], ids=["the bare mask", "the mask with the last four characters"])
async def test_a_provider_whose_token_is_a_served_mask_is_refused(toolset, sp, served: str) -> None:
    result = await toolset.call(tool_name="create_workspace_provider", arguments={"entity": _k8s(served)}, ctx=ADMIN_CALLER)

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert "re-enter" in result.output
    assert await sp.get_storage(WorkspaceProvider).get("wp-copy") is None, "nothing was stored"


@pytest.mark.asyncio
async def test_a_provider_with_a_real_token_is_created(toolset, sp) -> None:
    result = await toolset.call(tool_name="create_workspace_provider", arguments={"entity": _k8s("a-real-service-account-token")}, ctx=ADMIN_CALLER)

    assert not result.is_error, result.output
    stored = await sp.get_storage(WorkspaceProvider).get("wp-copy")
    assert stored.config.connection.token.get_secret_value() == "a-real-service-account-token"


@pytest.mark.asyncio
async def test_a_template_whose_env_value_is_a_served_mask_is_refused(toolset, sp) -> None:
    result = await toolset.call(tool_name="create_workspace_template", arguments={"entity": _template({"API_TOKEN": "**********"})}, ctx=ADMIN_CALLER)

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert "API_TOKEN" in result.output
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-copy") is None


@pytest.mark.asyncio
async def test_a_template_with_a_real_env_is_created(toolset, sp) -> None:
    result = await toolset.call(tool_name="create_workspace_template", arguments={"entity": _template({"API_TOKEN": "a-real-value"})}, ctx=ADMIN_CALLER)

    assert not result.is_error, result.output
    assert (await sp.get_storage(WorkspaceTemplate).get("tpl-copy")).env["API_TOKEN"].get_secret_value() == "a-real-value"
