"""Where a local-provider workspace may be used (ticket 01a1072f).

A local workspace's write lock (:mod:`primer.workspace._locks`) is in-process only, so two processes writing
one local workspace do not exclude each other. A docker or kubernetes workspace carries its own locking inside
its sandbox. The deployment therefore refuses a local workspace wherever more than one process could touch it.

The rule is static and testable, not a count of live workers (a worker can start after the workspace exists):

* DISTRIBUTED when ``runtime_mode`` is not ``api+worker`` (an API-only or worker-only process implies the other
  role lives elsewhere) OR the scheduler is Postgres (the claim state is shared, so another process may claim);
* the operator may assert ``local_workspaces.single_process`` when exactly one process runs everything on a Postgres
  scheduler, the one case the rule would otherwise refuse wrongly. It is logged at boot and shown on health;
* the whole refusal sits behind ``local_workspaces.refuse_when_distributed`` (default OFF) so it can ship before
  the existing local workspaces are migrated.

The single place it is enforced is :meth:`primer.api.registries.workspace_registry.WorkspaceRegistry.get_backend`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from primer.model.except_ import ConfigError
from primer.model.scheduler import RuntimeMode, SchedulerProviderType
from primer.model.workspace import (
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.model.workspace_refusal import WorkspaceRefusedError

logger = logging.getLogger(__name__)

_WAY_OUT = (
    "Use a workspace template on a docker or kubernetes provider (for example k3s-default), which carry their "
    "own locking. If exactly one process runs the API and the worker together, set "
    "local_workspaces.single_process to true."
)


def _signals(runtime_mode: RuntimeMode, scheduler_provider: SchedulerProviderType | None) -> tuple[str, ...]:
    signals: list[str] = []
    if runtime_mode != RuntimeMode.API_PLUS_WORKER:
        signals.append(
            f"runtime_mode is {runtime_mode.value!r}: the API and the worker are separate processes"
        )
    if scheduler_provider == SchedulerProviderType.POSTGRES:
        signals.append("the scheduler is postgres: its claim state is shared, so another process may claim a session")
    return tuple(signals)


@dataclass(frozen=True)
class LocalWorkspacePolicy:
    """Whether this process may hand out a workspace on a local provider.

    The default is the permissive one (switch off), so a registry built without a policy behaves as before.
    """

    enforce: bool = False
    single_process: bool = False
    signals: tuple[str, ...] = field(default=())

    @property
    def distributed(self) -> bool:
        return bool(self.signals)

    @property
    def refuses_local(self) -> bool:
        return self.enforce and self.distributed and not self.single_process

    @classmethod
    def from_topology(
        cls,
        *,
        runtime_mode: RuntimeMode,
        scheduler_provider: SchedulerProviderType | None,
        enforce: bool = False,
        single_process: bool = False,
    ) -> "LocalWorkspacePolicy":
        return cls(
            enforce=enforce,
            single_process=single_process,
            signals=_signals(runtime_mode, scheduler_provider),
        )

    def refusal(self, *, provider_id: str) -> WorkspaceRefusedError:
        """The error for a local provider, naming the signals that tripped and the way out."""
        return WorkspaceRefusedError(
            f"provider {provider_id!r} is a local workspace provider, which this deployment does not allow: "
            + "; ".join(self.signals)
            + f". A local workspace's write lock exists only inside one process. {_WAY_OUT}",
            provider_id=provider_id,
            signals=self.signals,
        )

    def check_provider(self, provider: WorkspaceProvider) -> None:
        """Raise :class:`WorkspaceRefusedError` when *provider* is a local one this deployment refuses."""
        if self.refuses_local and provider.provider == WorkspaceProviderType.LOCAL:
            raise self.refusal(provider_id=provider.id)

    def describe(self) -> dict[str, Any]:
        """The topology half of the health block (the counts are added by the health route)."""
        return {
            "distributed": self.distributed,
            "enforcing": self.enforce,
            "single_process": self.single_process,
            "refusing_local": self.refuses_local,
            "signals": list(self.signals),
        }

    def log_boot(self) -> None:
        """One boot line saying which way the rule is set, so an operator can see it without reading config."""
        if self.enforce and self.distributed and self.single_process:
            logger.warning(
                "local workspaces are ALLOWED by local_workspaces.single_process although this deployment looks "
                "distributed (%s); the operator asserts exactly one process uses them",
                "; ".join(self.signals),
            )
        elif self.refuses_local:
            logger.warning(
                "local workspaces are REFUSED: %s. Existing workspaces on a local provider are unusable until "
                "they are migrated to a docker or kubernetes provider.",
                "; ".join(self.signals),
            )
        elif self.distributed:
            logger.info(
                "this deployment looks distributed (%s) but local_workspaces.refuse_when_distributed is off, so "
                "local workspaces are still handed out",
                "; ".join(self.signals),
            )


async def validate_default_template(
    policy: LocalWorkspacePolicy, storage_provider: Any, template_id: str,
) -> None:
    """Fail the boot when the default workspace would be seeded on a provider the policy refuses.

    Only meaningful once the template row exists (bootstrap writes the reserved ones); an absent template is left
    to the seed, which defers it. Raises :class:`ConfigError`.
    """
    if not policy.refuses_local:
        return
    template = await storage_provider.get_storage(WorkspaceTemplate).get(template_id)
    if template is None:
        return
    provider = await storage_provider.get_storage(WorkspaceProvider).get(template.provider_id)
    if provider is None or provider.provider != WorkspaceProviderType.LOCAL:
        return
    raise ConfigError(
        f"default_workspace_template {template_id!r} is on the local provider {provider.id!r}, which this "
        "deployment refuses: " + "; ".join(policy.signals) + ". Point default_workspace_template at a template on "
        "a docker or kubernetes provider (for example k3s-default), or set local_workspaces.single_process."
    )


__all__ = [
    "LocalWorkspacePolicy",
    "WorkspaceRefusedError",
    "validate_default_template",
]
