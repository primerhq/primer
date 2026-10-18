"""``WorkspaceRegistry.destroy`` refuses the default workspace (pivot P2 Task 2).

The registry is the ONE funnel every workspace delete passes: the REST route and the ``delete_workspace`` tool both end in ``destroy``. So the
refusal lives here, as the first statement, and the layers above only translate the error (REST to a 403, the tool to ``forbidden``). The
default workspace is the one named by ``SystemState.default_workspace_id``, or, while that is unset, the legacy ``primer`` row.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.except_ import NotFoundError, PrimerError
from primer.model.workspace import (
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
)
from primer.model.workspace_refusal import ReservedWorkspaceError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests._support.reconcile_guard import reconcile_query_must_not_fail  # noqa: F401  (autouse: see the module)


class _Backend:
    """A backend that only records what it was asked to destroy."""

    def __init__(self) -> None:
        self.destroyed: list[str] = []

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def destroy(self, workspace_id: str) -> None:
        self.destroyed.append(workspace_id)


def _row(workspace_id: str) -> Workspace:
    return Workspace(
        id=workspace_id,
        template_id="tpl-1",
        provider_id="local",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
    )


class _Built:
    """The registry under test, its backend, and how many times its factory ran."""

    def __init__(self, sp) -> None:
        self.backend = _Backend()
        self.factory_calls = 0

        def factory(_provider: WorkspaceProvider) -> _Backend:
            self.factory_calls += 1
            return self.backend

        self.registry = WorkspaceRegistry(sp, factory=factory)  # type: ignore[arg-type]


async def _setup(sp, *workspace_ids: str) -> _Built:
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(
            id="local", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-reserved-ws"),
        )
    )
    for workspace_id in workspace_ids:
        await sp.get_storage(Workspace).create(_row(workspace_id))
    return _Built(sp)


async def test_destroy_refuses_the_default_workspace(fake_storage_provider) -> None:
    built = await _setup(fake_storage_provider, "prime")
    await fake_storage_provider.set_default_workspace_id("prime")

    with pytest.raises(ReservedWorkspaceError) as refused:
        await built.registry.destroy("prime")

    assert refused.value.workspace_id == "prime"
    assert built.backend.destroyed == [], "a refused destroy must not tear anything down"
    assert await fake_storage_provider.get_storage(Workspace).get("prime") is not None


async def test_destroy_refuses_a_legacy_default_before_the_field_is_recorded(fake_storage_provider) -> None:
    """An install that predates the column has no recorded id yet; its ``primer`` row is the default until the ensure pass records it."""
    built = await _setup(fake_storage_provider, "primer")
    assert (await fake_storage_provider.get_system_state()).default_workspace_id is None

    with pytest.raises(ReservedWorkspaceError):
        await built.registry.destroy("primer")

    assert built.backend.destroyed == []
    assert await fake_storage_provider.get_storage(Workspace).get("primer") is not None


async def test_destroy_of_another_workspace_still_works(fake_storage_provider) -> None:
    built = await _setup(fake_storage_provider, "prime", "ws-1")
    await fake_storage_provider.set_default_workspace_id("prime")

    await built.registry.destroy("ws-1")

    assert built.backend.destroyed == ["ws-1"]
    assert await fake_storage_provider.get_storage(Workspace).get("ws-1") is None
    assert await fake_storage_provider.get_storage(Workspace).get("prime") is not None


async def test_the_guard_is_the_first_statement_so_nothing_is_read_or_built(fake_storage_provider) -> None:
    """With the default recorded but neither its row nor its provider present, the answer is still the refusal (not a 404), and no backend is
    constructed: the check precedes the row read, the provider lookup and the backend."""
    await fake_storage_provider.set_default_workspace_id("prime")
    registry_and_backend = _Built(fake_storage_provider)

    with pytest.raises(ReservedWorkspaceError):
        await registry_and_backend.registry.destroy("prime")

    assert registry_and_backend.factory_calls == 0
    assert registry_and_backend.backend.destroyed == []


async def test_a_refused_destroy_leaves_the_default_workspaces_sessions_open(fake_storage_provider) -> None:
    """``destroy`` ends every open session ``workspace_lost`` once it goes ahead. The refusal comes first, so none of that happens."""
    built = await _setup(fake_storage_provider, "prime")
    await fake_storage_provider.set_default_workspace_id("prime")
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(
        WorkspaceSession(
            id="sess-open",
            workspace_id="prime",
            binding=AgentSessionBinding(agent_id="operator"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
    )

    with pytest.raises(ReservedWorkspaceError):
        await built.registry.destroy("prime")

    still = await sessions.get("sess-open")
    assert still.status == SessionStatus.RUNNING
    assert still.ended_reason is None


async def test_the_refusal_names_the_workspace_and_its_problem_extensions(fake_storage_provider) -> None:
    built = await _setup(fake_storage_provider, "prime")
    await fake_storage_provider.set_default_workspace_id("prime")

    with pytest.raises(ReservedWorkspaceError) as refused:
        await built.registry.destroy("prime")

    error = refused.value
    assert isinstance(error, PrimerError)
    assert error.message == "workspace 'prime' is the default workspace (prime) and cannot be deleted"
    assert error.problem_extensions == {"error": "reserved_workspace", "workspace_id": "prime"}


async def test_the_legacy_fallback_applies_only_while_the_field_is_unset(fake_storage_provider) -> None:
    """Once ``prime`` is recorded, a leftover ``primer`` row is an ordinary workspace: only the recorded id is protected."""
    built = await _setup(fake_storage_provider, "prime", "primer")
    await fake_storage_provider.set_default_workspace_id("prime")

    await built.registry.destroy("primer")

    assert built.backend.destroyed == ["primer"]
    with pytest.raises(ReservedWorkspaceError):
        await built.registry.destroy("prime")


async def test_an_unknown_workspace_is_still_not_found(fake_storage_provider) -> None:
    """The guard does not swallow the ordinary 404: an id that is neither the default nor a row stays a ``NotFoundError``."""
    built = await _setup(fake_storage_provider, "prime")
    await fake_storage_provider.set_default_workspace_id("prime")

    with pytest.raises(NotFoundError):
        await built.registry.destroy("no-such")
