"""ContainerRuntimeAdapter ABC.

The abstraction over an OCI-style container runtime. Three concrete
adapters ship in Phase B: Docker (aiodocker), Podman (aiohttp),
containerd (CRI). Each adapter knows how to create / list / look up /
destroy named sandboxes in its runtime, and returns a :class:`Sandbox`
handle that implements file + exec operations against that specific
runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from primer.int.sandbox import Sandbox
from primer.model.workspace import ResourceLimits, VolumeMount


class ContainerRuntimeAdapter(ABC):
    """Abstract OCI-runtime adapter. One impl per backend (Docker / Podman / CRI)."""

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    @abstractmethod
    async def create_sandbox(
        self,
        *,
        name: str,
        image: str,
        command: list[str],
        env: dict[str, str],
        workdir: str,
        volume_name: str,
        volume_target: str,
        extra_mounts: list[VolumeMount],
        user: str | None,
        resources: ResourceLimits,
        network: Literal["none", "egress", "full"],
        pull_policy: Literal["always", "if_missing", "never"],
    ) -> Sandbox:
        """Pull image (per ``pull_policy``), create named volume, create
        container, start it, return a Sandbox handle."""

    @abstractmethod
    async def get_sandbox(self, name: str) -> Sandbox | None:
        """Look up a sandbox by name. Starts it if stopped. Returns
        ``None`` if no sandbox by that name exists."""

    @abstractmethod
    async def list_sandboxes(self) -> list[str]:
        """Return names of sandboxes created by this adapter (typically
        filtered by the configured ``name_prefix`` label)."""

    @abstractmethod
    async def remove_volume(self, name: str) -> None:
        """Remove a named volume. Called from
        :meth:`ContainerWorkspaceBackend.destroy` after sandbox removal."""


__all__ = ["ContainerRuntimeAdapter"]
