"""Where a workspace on a local provider may be used (ticket 01a1072f).

A local workspace's write lock is in-process only, so a deployment whose API and worker are separate processes (or
whose scheduler is Postgres, so any process may claim) refuses to hand one out, behind a switch that is OFF by
default. These tests pin the rule, its single enforcement point (``WorkspaceRegistry.get_backend``), the way out for
an operator (destroy, the single-process opt-in) and the boot validation of the default template.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from primer.api.config import AppConfig
from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.except_ import ConfigError, ConflictError
from primer.model.scheduler import RuntimeMode, SchedulerProviderType
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.workspace.local_policy import LocalWorkspacePolicy, validate_default_template

API, WORKER, BOTH = RuntimeMode.API, RuntimeMode.WORKER, RuntimeMode.API_PLUS_WORKER
POSTGRES, MEMORY = SchedulerProviderType.POSTGRES, SchedulerProviderType.IN_MEMORY


def _policy(
    mode: RuntimeMode = BOTH,
    scheduler: SchedulerProviderType | None = None,
    *,
    enforce: bool = True,
    single_process: bool = False,
) -> LocalWorkspacePolicy:
    return LocalWorkspacePolicy.from_topology(
        runtime_mode=mode, scheduler_provider=scheduler, enforce=enforce, single_process=single_process,
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "scheduler", "distributed"),
    [
        (BOTH, None, False),
        (BOTH, MEMORY, False),
        (API, None, True),
        (WORKER, None, True),
        (BOTH, POSTGRES, True),
        (API, POSTGRES, True),
        (WORKER, POSTGRES, True),
    ],
)
def test_distributed_is_a_non_single_process_mode_or_a_postgres_scheduler(mode, scheduler, distributed) -> None:
    policy = _policy(mode, scheduler)
    assert policy.distributed is distributed
    assert policy.refuses_local is distributed


def test_the_switch_off_never_refuses_whatever_the_topology() -> None:
    policy = _policy(WORKER, POSTGRES, enforce=False)
    assert policy.distributed is True
    assert policy.refuses_local is False


def test_the_single_process_opt_in_allows_local_on_a_postgres_scheduler() -> None:
    assert _policy(BOTH, POSTGRES, single_process=True).refuses_local is False


def test_each_tripped_signal_is_named_so_the_error_can_say_why() -> None:
    both = _policy(WORKER, POSTGRES)
    assert len(both.signals) == 2
    assert any("runtime_mode" in s and "worker" in s for s in both.signals)
    assert any("postgres" in s for s in both.signals)
    assert _policy(BOTH, POSTGRES).signals == (
        "the scheduler is postgres: its claim state is shared, so another process may claim a session",
    )


def test_the_default_policy_is_permissive() -> None:
    assert LocalWorkspacePolicy().refuses_local is False


def test_describe_reports_the_topology_even_with_the_switch_off() -> None:
    described = _policy(WORKER, POSTGRES, enforce=False).describe()
    assert described["distributed"] is True
    assert described["enforcing"] is False
    assert described["refusing_local"] is False
    assert len(described["signals"]) == 2


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------


def test_app_config_defaults_the_switch_and_the_opt_in_off() -> None:
    config = AppConfig()
    assert config.local_workspaces.refuse_when_distributed is False
    assert config.local_workspaces.single_process is False
    assert config.local_workspace_policy().refuses_local is False


def test_app_config_builds_the_policy_from_its_topology_and_switch() -> None:
    config = AppConfig(
        runtime_mode="worker",
        scheduler={"provider": "postgres", "config": {"dsn": "postgresql://x/y"}},
        local_workspaces={"refuse_when_distributed": True},
    )
    policy = config.local_workspace_policy()
    assert policy.refuses_local is True
    assert len(policy.signals) == 2


def test_app_config_in_memory_single_process_is_not_distributed() -> None:
    config = AppConfig(local_workspaces={"refuse_when_distributed": True})
    assert config.local_workspace_policy().distributed is False


# ---------------------------------------------------------------------------
# The gate: WorkspaceRegistry.get_backend
# ---------------------------------------------------------------------------


class _Storage:
    def __init__(self, *rows: Any) -> None:
        self.rows = {r.id: r for r in rows}

    async def get(self, id: str) -> Any:
        return self.rows.get(id)

    async def delete(self, id: str) -> None:
        del self.rows[id]


class _SP:
    def __init__(self, **by_model: Any) -> None:
        self._by_model = by_model

    def get_storage(self, model: type) -> _Storage:
        return self._by_model.setdefault(model.__name__, _Storage())


class _Backend:
    def __init__(self) -> None:
        self.initialised = False
        self.destroyed: list[str] = []
        self.gets: list[str] = []

    async def initialize(self) -> None:
        self.initialised = True

    async def aclose(self) -> None:
        pass

    async def get(self, workspace_id: str, *, template: Any = None) -> Any:
        self.gets.append(workspace_id)
        return f"live:{workspace_id}"

    async def create(self, template: Any, **kwargs: Any) -> Any:
        return f"created:{kwargs.get('workspace_id')}"

    async def destroy(self, workspace_id: str) -> None:
        self.destroyed.append(workspace_id)


LOCAL = WorkspaceProvider(
    id="local", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-policy"),
)
DOCKER = WorkspaceProvider(
    id="docker", provider=WorkspaceProviderType.CONTAINER,
    config=ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(bind_host="127.0.0.1"),
    ),
)


def _registry(policy: LocalWorkspacePolicy, *extra: Any) -> tuple[WorkspaceRegistry, list[WorkspaceProvider]]:
    built: list[WorkspaceProvider] = []

    def factory(provider: WorkspaceProvider) -> _Backend:
        built.append(provider)
        return _Backend()

    sp = _SP(
        WorkspaceProvider=_Storage(LOCAL, DOCKER),
        WorkspaceTemplate=_Storage(
            WorkspaceTemplate(id="local-tpl", description="t", provider_id="local", backend={"kind": "local"}),
        ),
        Workspace=_Storage(*extra),
    )
    return WorkspaceRegistry(sp, factory=factory, local_policy=policy), built


def _row(workspace_id: str = "w1", provider_id: str = "local") -> WorkspaceRow:
    return WorkspaceRow(
        id=workspace_id, template_id="local-tpl", provider_id=provider_id,
        created_at=datetime.now(timezone.utc), phase="running",
        runtime_meta={"url": "ws://unused", "token": "unused"},
    )


async def test_a_refused_local_provider_raises_and_never_builds_a_backend() -> None:
    registry, built = _registry(_policy(WORKER, POSTGRES))
    with pytest.raises(WorkspaceRefusedError) as raised:
        await registry.get_backend("local")
    assert built == [], "the refused provider's backend must not even be constructed (it would create directories)"
    error = raised.value
    assert error.provider_id == "local"
    assert "local" in error.message and "runtime_mode" in error.message and "postgres" in error.message
    assert "k3s-default" in error.message and "single_process" in error.message
    assert isinstance(error, ConflictError)


async def test_a_docker_provider_is_never_refused() -> None:
    registry, built = _registry(_policy(WORKER, POSTGRES))
    assert await registry.get_backend("docker") is not None
    assert [p.id for p in built] == ["docker"]


@pytest.mark.parametrize(
    "policy",
    [
        _policy(WORKER, POSTGRES, enforce=False),
        _policy(BOTH, None),
        _policy(BOTH, POSTGRES, single_process=True),
    ],
    ids=["switch-off", "single-process-topology", "single-process-opt-in"],
)
async def test_a_local_provider_is_handed_out_when_the_policy_allows_it(policy) -> None:
    registry, built = _registry(policy)
    assert await registry.get_backend("local") is not None
    assert [p.id for p in built] == ["local"]


async def test_get_workspace_on_a_refused_provider_names_the_workspace() -> None:
    registry, _ = _registry(_policy(WORKER, POSTGRES), _row("w1"))
    with pytest.raises(WorkspaceRefusedError) as raised:
        await registry.get_workspace("w1")
    assert raised.value.workspace_id == "w1"
    assert "'w1'" in raised.value.message
    assert raised.value.provider_id == "local"


async def test_check_workspace_allowed_refuses_a_local_workspace_without_building_anything() -> None:
    registry, built = _registry(_policy(WORKER, POSTGRES), _row("w1"))
    with pytest.raises(WorkspaceRefusedError) as raised:
        await registry.check_workspace_allowed("w1")
    assert raised.value.workspace_id == "w1" and "'w1'" in raised.value.message
    assert built == [], "a policy-only look must never build or attach a backend"


async def test_check_workspace_allowed_passes_a_docker_workspace() -> None:
    registry, built = _registry(_policy(WORKER, POSTGRES), _row("w2", provider_id="docker"))
    await registry.check_workspace_allowed("w2")
    assert built == []


async def test_check_workspace_allowed_costs_nothing_while_the_policy_refuses_nothing() -> None:
    """Not even a read of the workspace row: an unknown id is not the policy's business when nothing is refused."""
    registry, _ = _registry(_policy(BOTH, None))
    await registry.check_workspace_allowed("no-such-workspace")


async def test_materialise_on_a_refused_provider_creates_nothing() -> None:
    registry, built = _registry(_policy(API))
    template = WorkspaceTemplate(
        id="local-tpl", description="t", provider_id="local", backend={"kind": "local"},
    )
    with pytest.raises(WorkspaceRefusedError):
        await registry.materialise(template=template, workspace_id="new")
    assert built == []


async def test_destroying_a_refused_workspace_drops_its_row_and_ends_its_sessions() -> None:
    registry, built = _registry(_policy(WORKER, POSTGRES), _row("w1"))
    with patch(
        "primer.api.registries.workspace_registry.reconcile_sessions_to_workspace_lost", new=AsyncMock(),
    ) as reconcile:
        await registry.destroy("w1")
    assert built == [], "an abandon must not build the refused backend"
    reconcile.assert_awaited_once()
    assert await registry._sp.get_storage(WorkspaceRow).get("w1") is None


async def test_the_refusal_is_a_409_problem_with_its_own_type() -> None:
    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from primer.api.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise _policy(WORKER, POSTGRES).refusal(provider_id="local").for_workspace("w1")

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/boom")
    assert response.status_code == 409
    body = response.json()
    assert body["type"] == "/errors/workspace-refused"
    extensions = body["extensions"]
    assert extensions["workspace_id"] == "w1" and extensions["provider_id"] == "local"
    assert len(extensions["signals"]) == 2
    assert "k3s-default" in body["detail"]


# ---------------------------------------------------------------------------
# Boot validation of default_workspace_template
# ---------------------------------------------------------------------------


def _templates(*pairs: tuple[str, str]) -> _SP:
    return _SP(
        WorkspaceProvider=_Storage(LOCAL, DOCKER),
        WorkspaceTemplate=_Storage(*[
            WorkspaceTemplate(
                id=tid, description="t", provider_id=pid,
                backend={"kind": "local"} if pid == "local" else {"kind": "container", "image": "x:1"},
            )
            for tid, pid in pairs
        ]),
    )


async def test_boot_fails_when_the_default_template_is_on_a_refused_local_provider() -> None:
    with pytest.raises(ConfigError, match="default_workspace_template 'local-default'.*k3s-default"):
        await validate_default_template(
            _policy(WORKER, POSTGRES), _templates(("local-default", "local")), "local-default",
        )


async def test_boot_accepts_a_default_template_on_a_docker_provider() -> None:
    await validate_default_template(
        _policy(WORKER, POSTGRES), _templates(("k3s-default", "docker")), "k3s-default",
    )


async def test_boot_does_not_validate_when_the_policy_allows_local() -> None:
    sp = _templates(("local-default", "local"))
    for policy in (_policy(WORKER, POSTGRES, enforce=False), _policy(BOTH, None), _policy(BOTH, POSTGRES, single_process=True)):
        await validate_default_template(policy, sp, "local-default")


async def test_boot_leaves_an_absent_template_to_the_seed() -> None:
    await validate_default_template(_policy(WORKER, POSTGRES), _templates(), "nope")


# ---------------------------------------------------------------------------
# The boot line says which way the rule is set
# ---------------------------------------------------------------------------


def _boot_lines(policy: LocalWorkspacePolicy, caplog) -> list[tuple[int, str]]:
    with caplog.at_level(logging.INFO, logger="primer.workspace.local_policy"):
        policy.log_boot()
    return [(r.levelno, r.getMessage()) for r in caplog.records]


def test_the_boot_warns_that_local_workspaces_are_refused(caplog) -> None:
    [(level, message)] = _boot_lines(_policy(WORKER, POSTGRES), caplog)
    assert level == logging.WARNING
    assert "REFUSED" in message and "migrated" in message and "runtime_mode" in message


def test_the_boot_warns_when_the_operator_overrides_a_distributed_looking_deployment(caplog) -> None:
    [(level, message)] = _boot_lines(_policy(BOTH, POSTGRES, single_process=True), caplog)
    assert level == logging.WARNING
    assert "single_process" in message and "ALLOWED" in message and "postgres" in message


def test_the_boot_only_notes_a_distributed_deployment_whose_switch_is_off(caplog) -> None:
    [(level, message)] = _boot_lines(_policy(WORKER, POSTGRES, enforce=False), caplog)
    assert level == logging.INFO
    assert "refuse_when_distributed is off" in message


def test_the_boot_says_nothing_for_a_single_process_deployment(caplog) -> None:
    assert _boot_lines(_policy(BOTH, None), caplog) == []
    assert _boot_lines(_policy(BOTH, None, enforce=False), caplog) == []
