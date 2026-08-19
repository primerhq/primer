"""S5 P3: one idempotent pass, independent of the bootstrap marker."""
from __future__ import annotations

from datetime import datetime, timezone

from primer.bootstrap.defaults import (
    RESERVED_DEFAULT_WORKSPACE,
    RESERVED_LOCAL_WORKSPACE_TEMPLATE,
    RESERVED_OPERATOR_AGENT,
    RESERVED_WORKSPACE_TEMPLATES,
)
from primer.bootstrap.seed import crud_policy_id, run_ensure_pass
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.workspace import (
    Workspace,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)


class _LiveWorkspace:
    id = RESERVED_DEFAULT_WORKSPACE
    runtime_meta = WorkspaceRuntimeMeta(
        url="ws://127.0.0.1:5959", token="stub-token",
    )


class _Registry:
    async def materialise(self, *, template, overrides=None, workspace_id=None):
        return _LiveWorkspace()


async def _prepare(sp):
    sp.get_storage(ModelProfile)._data.clear()
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(
            **RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE]
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="llm-1--qwen",
            description="default",
            provider_id="llm-1",
            model_name="qwen",
            context_length=32000,
        )
    )


async def test_pass_runs_even_when_the_bootstrap_marker_is_stamped(
    fake_storage_provider,
):
    await fake_storage_provider.set_bootstrap_completed(
        datetime.now(timezone.utc)
    )
    await _prepare(fake_storage_provider)
    result = await run_ensure_pass(
        fake_storage_provider, workspace_registry=_Registry(),
    )
    assert result.errors == []
    assert RESERVED_OPERATOR_AGENT in result.created
    assert RESERVED_DEFAULT_WORKSPACE in result.created
    assert crud_policy_id("create_agent") in result.created


async def test_pass_is_idempotent(fake_storage_provider):
    await _prepare(fake_storage_provider)
    await run_ensure_pass(fake_storage_provider, workspace_registry=_Registry())
    second = await run_ensure_pass(
        fake_storage_provider, workspace_registry=_Registry(),
    )
    assert second.created == []
    assert second.errors == []


async def test_a_failing_step_does_not_stop_the_others(fake_storage_provider):
    class _Boom:
        async def materialise(self, *, template, overrides=None, workspace_id=None):
            raise RuntimeError("backend down")

    await _prepare(fake_storage_provider)
    result = await run_ensure_pass(
        fake_storage_provider, workspace_registry=_Boom(),
    )
    assert [step for step, _ in result.errors] == ["default_workspace"]
    assert await fake_storage_provider.get_storage(Agent).get(
        RESERVED_OPERATOR_AGENT
    ) is not None
    assert await fake_storage_provider.get_storage(Workspace).get(
        RESERVED_DEFAULT_WORKSPACE
    ) is None
    assert await fake_storage_provider.get_storage(ToolApprovalPolicy).get(
        crud_policy_id("create_agent")
    ) is not None
