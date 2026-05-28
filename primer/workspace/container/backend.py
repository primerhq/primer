"""ContainerWorkspaceBackend -- workspace lifecycle over a
ContainerRuntimeAdapter.

The runtime adapter (Docker / Podman / containerd) is selected by the
provider config's ``runtime.kind`` discriminator. Concrete adapters
land in :mod:`primer.workspace.runtime.docker` / ``podman`` /
``containerd`` -- imported lazily inside :func:`_adapter_for` so that
``ContainerWorkspaceBackend`` is unit-testable with a fake adapter
without needing those modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from primer.int.workspace import Workspace, WorkspaceBackend
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.workspace import (
    ContainerWorkspaceConfig,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
    ContainerTemplateConfig,
)
from primer.workspace.runtime.adapter import ContainerRuntimeAdapter
from primer.workspace.sandbox.workspace import SandboxWorkspace


logger = logging.getLogger(__name__)


def _generate_workspace_id() -> str:
    return f"ws-{uuid.uuid4().hex[:16]}"


def _host_uid_gid() -> str | None:
    """Best-effort host-UID:GID for container ``user``. Returns ``None``
    on Windows."""
    try:
        return f"{os.getuid()}:{os.getgid()}"  # type: ignore[attr-defined]
    except AttributeError:
        return None


def _adapter_for(runtime_cfg) -> ContainerRuntimeAdapter:
    """Build the matching adapter. Imports are deferred so that this
    module loads cleanly even when the optional runtime libraries
    aren't installed."""
    if runtime_cfg.kind == "docker":
        from primer.workspace.runtime.docker import DockerRuntimeAdapter
        return DockerRuntimeAdapter(runtime_cfg)
    if runtime_cfg.kind == "podman":
        from primer.workspace.runtime.podman import PodmanRuntimeAdapter
        return PodmanRuntimeAdapter(runtime_cfg)
    if runtime_cfg.kind == "containerd":
        from primer.workspace.runtime.containerd.adapter import (
            ContainerdRuntimeAdapter,
        )
        return ContainerdRuntimeAdapter(runtime_cfg)
    raise ConfigError(f"unknown runtime kind {runtime_cfg.kind!r}")


class ContainerWorkspaceBackend(WorkspaceBackend):
    """Materialises workspaces as long-lived containers."""

    def __init__(
        self,
        config: ContainerWorkspaceConfig,
        *,
        adapter: ContainerRuntimeAdapter | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter if adapter is not None else _adapter_for(
            config.runtime,
        )
        self._workspaces: dict[str, SandboxWorkspace] = {}
        self._lock = asyncio.Lock()
        self._initialised = False

    async def initialize(self) -> None:
        await self._adapter.initialize()
        self._initialised = True

    async def aclose(self) -> None:
        async with self._lock:
            for ws in list(self._workspaces.values()):
                try:
                    await ws.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("aclose on workspace failed: %s", exc)
            self._workspaces.clear()
            await self._adapter.aclose()
            self._initialised = False

    async def create(
        self,
        template: WorkspaceTemplate,
        *,
        overrides: WorkspaceTemplateOverrides | None = None,
    ) -> Workspace:
        if not isinstance(template.backend, ContainerTemplateConfig):
            raise ConfigError(
                f"ContainerWorkspaceBackend requires template backend kind "
                f"'container', got {template.backend.kind!r}"
            )
        spec = template.backend

        env: dict = dict(template.env)
        files = list(template.files)
        init_cmds = list(template.init_commands)
        if overrides is not None:
            env.update(overrides.env)
            files = files + list(overrides.files)
            init_cmds = init_cmds + list(overrides.init_commands)

        env_str = {k: v.get_secret_value() for k, v in env.items()}

        workspace_id = _generate_workspace_id()
        name = f"{self._config.name_prefix}{workspace_id}"
        volume = f"{name}-data"
        image = spec.image or self._config.default_image
        if image is None:
            raise ConfigError(
                "template has no image and provider has no default_image"
            )

        sandbox = await self._adapter.create_sandbox(
            name=name,
            image=image,
            command=spec.entrypoint or ["sleep", "infinity"],
            env=env_str,
            workdir=spec.workdir,
            volume_name=volume,
            volume_target=spec.workdir,
            extra_mounts=spec.extra_mounts,
            user=spec.user or _host_uid_gid(),
            resources=template.resources,
            network=template.resources.network,
            pull_policy=self._config.pull_policy,
        )

        try:
            for fm in files:
                if fm.source.kind == "inline":
                    await sandbox.write_file(
                        f"{spec.workdir}/{fm.path}",
                        fm.source.content.encode("utf-8"),
                    )
                else:
                    logger.warning(
                        "non-inline file source not yet supported",
                        extra={"path": fm.path, "kind": fm.source.kind},
                    )
            for cmd in init_cmds:
                res = await sandbox.exec(
                    cmd, workdir=spec.workdir, env=env_str,
                )
                if res.exit_code != 0:
                    raise ConfigError(
                        f"init command failed (rc={res.exit_code}): "
                        f"{cmd!r}\nstderr: {res.stderr}"
                    )
            ws = await SandboxWorkspace.materialise(
                workspace_id=workspace_id,
                template=template,
                sandbox=sandbox,
                backend_kind="container",
                workspace_root=spec.workdir,
            )
        except Exception:
            try:
                await sandbox.remove()
            except Exception as exc:  # noqa: BLE001
                logger.warning("rollback remove failed: %s", exc)
            try:
                await self._adapter.remove_volume(volume)
            except Exception as exc:  # noqa: BLE001
                logger.warning("rollback volume remove failed: %s", exc)
            raise

        async with self._lock:
            self._workspaces[workspace_id] = ws
        return ws

    async def get(
        self,
        workspace_id: str,
        *,
        template: WorkspaceTemplate | None = None,
    ) -> Workspace | None:
        cached = self._workspaces.get(workspace_id)
        if cached is not None:
            return cached
        name = f"{self._config.name_prefix}{workspace_id}"
        sandbox = await self._adapter.get_sandbox(name)
        if sandbox is None:
            return None
        # Lazy re-attach: the sandbox is alive (started by the adapter
        # if it had stopped). Without a persisted template we cannot
        # rebuild the SandboxWorkspace wrapper; the caller will see
        # None and can re-issue the call with the template.
        if template is None:
            logger.debug(
                "ContainerWorkspaceBackend.get: sandbox %r exists but no "
                "template supplied for re-attach; returning None",
                name,
            )
            return None
        if not isinstance(template.backend, ContainerTemplateConfig):
            raise ConfigError(
                f"re-attach for workspace {workspace_id!r}: template "
                f"backend kind is {template.backend.kind!r}, expected "
                "'container'"
            )
        ws = await SandboxWorkspace.materialise(
            workspace_id=workspace_id,
            template=template,
            sandbox=sandbox,
            backend_kind="container",
            workspace_root=template.backend.workdir,
        )
        async with self._lock:
            # Race: another caller may have built the same wrapper.
            existing = self._workspaces.get(workspace_id)
            if existing is not None:
                # Discard our wrapper; theirs wins. Don't tear the
                # sandbox down -- it's the same one.
                return existing
            self._workspaces[workspace_id] = ws
        return ws

    async def list(self) -> list[str]:
        names = await self._adapter.list_sandboxes()
        return [
            n.removeprefix(self._config.name_prefix) for n in names
            if n.startswith(self._config.name_prefix)
        ]

    async def destroy(self, workspace_id: str) -> None:
        async with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
        name = f"{self._config.name_prefix}{workspace_id}"
        volume = f"{name}-data"
        if ws is not None:
            sandbox = ws.sandbox
        else:
            sandbox = await self._adapter.get_sandbox(name)
        if sandbox is None:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        await sandbox.stop()
        await sandbox.remove()
        await self._adapter.remove_volume(volume)


__all__ = ["ContainerWorkspaceBackend"]
