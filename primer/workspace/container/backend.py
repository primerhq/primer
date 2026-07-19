"""ContainerWorkspaceBackend -- workspace lifecycle over a
ContainerRuntimeAdapter.

The runtime adapter (Docker / Podman / containerd) is selected by the
provider config's ``runtime`` discriminator. Concrete adapters land in
:mod:`primer.workspace.runtime.docker` / ``podman`` / ``containerd`` --
imported lazily inside :func:`_adapter_for` so that
``ContainerWorkspaceBackend`` is unit-testable with a fake adapter
without needing those modules.

``create()`` honours the provider's :class:`ContainerReachabilityConfig`:

* ``host_port`` -- ask the adapter to publish 5959 on the configured
  ``bind_host`` (random host port). After start, the adapter reports
  the discovered port back on the returned sandbox; we feed that into
  :func:`build_runtime_url` so the runtime client URL matches.
* ``bridge_network`` -- ask the adapter to attach the container to the
  shared docker network and name it ``workspace-<workspace_id>``. The
  URL is purely DNS-based; no port lookup is needed.

A fresh per-workspace bearer token (``RUNTIME_TOKEN``) is minted before
the container starts and handed to the adapter, which injects it as a
container env var AND opens a :class:`RuntimeClient` against the
runtime listening inside. This mirrors the K8s backend's flow so the
two backends present the same handshake to primer-runtime; Task 6.3
will persist the URL+token on the workspace row for re-attach.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid

from primer.int.workspace import Workspace
from primer.model.except_ import ConfigError, NotFoundError
from pydantic import SecretStr

from primer.model.workspace import (
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    ResourceLimits,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace.base_backend import BaseWorkspaceBackend
from primer.workspace.files import FileResolvers
from primer.workspace.runtime.adapter import ContainerRuntimeAdapter
from primer.workspace.runtime.url import build_runtime_url
from primer.workspace.sandbox.workspace import SandboxWorkspace


logger = logging.getLogger(__name__)


_NAME_PREFIX = "workspace-"


def _generate_workspace_id() -> str:
    return f"ws-{uuid.uuid4().hex[:16]}"


def _host_uid_gid() -> str | None:
    """Best-effort host-UID:GID for container ``user``. Returns ``None``
    on Windows."""
    try:
        return f"{os.getuid()}:{os.getgid()}"  # type: ignore[attr-defined]
    except AttributeError:
        return None


def _adapter_for(cfg: ContainerWorkspaceConfig) -> ContainerRuntimeAdapter:
    """Build the matching adapter. Imports are deferred so that this
    module loads cleanly even when the optional runtime libraries
    aren't installed."""
    if cfg.runtime == "docker":
        from primer.workspace.runtime.docker import DockerRuntimeAdapter
        return DockerRuntimeAdapter(cfg)
    if cfg.runtime == "podman":
        from primer.workspace.runtime.podman import PodmanRuntimeAdapter
        return PodmanRuntimeAdapter(cfg)
    if cfg.runtime == "containerd":
        from primer.workspace.runtime.containerd.adapter import (
            ContainerdRuntimeAdapter,
        )
        return ContainerdRuntimeAdapter(cfg)
    raise ConfigError(f"unknown runtime kind {cfg.runtime!r}")


class ContainerWorkspaceBackend(BaseWorkspaceBackend):
    """Materialises workspaces as long-lived containers."""

    def __init__(
        self,
        config: ContainerWorkspaceConfig,
        *,
        adapter: ContainerRuntimeAdapter | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._adapter = adapter if adapter is not None else _adapter_for(
            config,
        )
        self._workspaces: dict[str, SandboxWorkspace] = {}

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
        workspace_id: str | None = None,
        resolvers: FileResolvers | None = None,
    ) -> Workspace:
        if not isinstance(template.backend, ContainerTemplateConfig):
            raise ConfigError(
                f"ContainerWorkspaceBackend requires template backend kind "
                f"'container', got {template.backend.kind!r}"
            )
        spec = template.backend

        merged = self.merge_overrides(template, overrides)
        files = merged.files
        init_cmds = merged.init_commands
        env_str = merged.env_unwrapped()
        if template.strict_write_locking:
            env_str = {**env_str, "PRIMER_STRICT_WRITE_LOCKING": "1"}

        # Pin the live instance to the caller-supplied id when given so the
        # durable row id and the container name/id agree -- otherwise
        # re-attach after cache eviction looks up the wrong id and 404s.
        workspace_id = workspace_id or _generate_workspace_id()
        # ``workspace-<id>`` is also the docker container's hostname; the
        # bridge_network URL builder reaches the runtime via this exact
        # name. Keep the prefix shared between both reachability modes so
        # ``list()`` works uniformly.
        name = f"{_NAME_PREFIX}{workspace_id}"
        volume = f"{name}-data"
        image = spec.image
        if not image:
            raise ConfigError("template has no image")

        token = secrets.token_urlsafe(32)
        reachability = self._config.reachability

        # Resource limits live on the template in the redesigned model;
        # build the legacy ResourceLimits shape the adapter still consumes.
        resources = ResourceLimits(
            cpu_cores=spec.cpu_cores,
            memory_bytes=spec.memory_bytes,
            network="full",
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
            resources=resources,
            network="full",
            pull_policy="if_missing",
            reachability=reachability,
            token=token,
        )

        try:
            # For host_port mode, the adapter discovered the mapped host
            # port after start and stashed it on the sandbox; pick it up
            # so the URL we build below matches what the platform can
            # actually reach.
            mapped_host_port: int | None = None
            if isinstance(reachability, ContainerReachabilityHostPort):
                mapped_host_port = getattr(sandbox, "mapped_host_port", None)
                if mapped_host_port is None:
                    raise ConfigError(
                        "host_port reachability: adapter did not report "
                        "a mapped_host_port back to the backend"
                    )
            # Build the platform-side URL via the same helper the K8s
            # backend uses. The real Docker adapter has already wired a
            # :class:`RuntimeClient` against this same URL+token inside
            # the returned :class:`WSSandbox` -- we re-derive the URL
            # here purely for assertion + observability (Task 6.3 will
            # persist it on the workspace row). Test adapters that
            # return a non-WSSandbox handle (e.g. ``FakeSandbox``) skip
            # the runtime-client step entirely since there's no real
            # process to talk to.
            url = build_runtime_url(
                provider_config=self._config,
                workspace_id=workspace_id,
                mapped_host_port=mapped_host_port,
            )
            logger.debug(
                "container workspace %s URL=%s (token redacted)",
                workspace_id, url,
            )

            # Resolve every FileSource variant (inline/url/document/secret)
            # up-front via the shared helper; the sandbox writes the
            # resulting bytes.
            async def _write(rf):
                await sandbox.write_file(
                    f"{spec.workdir}/{rf.path}",
                    rf.content,
                    mode=int(rf.mode, 8) if rf.mode else None,
                )

            await self.materialize_files_on_backend(
                files, _write, resolvers=resolvers,
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
            runtime_meta = WorkspaceRuntimeMeta(
                url=url,
                token=SecretStr(token),
                mapped_host_port=mapped_host_port,
            )
            ws = await SandboxWorkspace.materialise(
                workspace_id=workspace_id,
                template=template,
                sandbox=sandbox,
                backend_kind="container",
                runtime_meta=runtime_meta,
                workspace_root=spec.workdir,
            )
        except Exception:
            # Close the inner RuntimeClient (WS + aiohttp session) FIRST:
            # remove() only deletes the daemon-side container + volume and
            # would otherwise leak the in-process connection on rollback.
            aclose = getattr(sandbox, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("rollback sandbox aclose failed: %s", exc)
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

    async def _reattach(
        self,
        workspace_id: str,
        template: WorkspaceTemplate | None,
    ) -> Workspace | None:
        """Re-attach to a live container after a cache miss.

        Called by :meth:`BaseWorkspaceBackend.get` only once the cache
        lookup (with gone-eviction) misses.
        """
        name = f"{_NAME_PREFIX}{workspace_id}"
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
        # Re-attach: rebuild the runtime_meta so the wrapper still
        # exposes a non-None ``runtime_meta`` per the Workspace ABC. The
        # adapter recovered the bearer token from the container env
        # (``docker inspect`` -> ``Config.Env``) and stashed it on the
        # sandbox as ``recovered_token``; fold it back into the meta so the
        # re-attached workspace carries the live token (mirrors the K8s
        # backend recovering it from the per-workspace Secret). Falls back
        # to an empty SecretStr when the adapter could not recover it.
        reattach_host_port: int | None = None
        if isinstance(self._config.reachability, ContainerReachabilityHostPort):
            reattach_host_port = getattr(sandbox, "mapped_host_port", None)
        reattach_url = build_runtime_url(
            provider_config=self._config,
            workspace_id=workspace_id,
            mapped_host_port=reattach_host_port,
        )
        recovered_token = getattr(sandbox, "recovered_token", None)
        runtime_meta = WorkspaceRuntimeMeta(
            url=reattach_url,
            token=SecretStr(recovered_token) if recovered_token else SecretStr(""),
            mapped_host_port=reattach_host_port,
        )
        ws = await SandboxWorkspace.materialise(
            workspace_id=workspace_id,
            template=template,
            sandbox=sandbox,
            backend_kind="container",
            runtime_meta=runtime_meta,
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
            n.removeprefix(_NAME_PREFIX) for n in names
            if n.startswith(_NAME_PREFIX)
        ]

    async def destroy(self, workspace_id: str) -> None:
        async with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
        name = f"{_NAME_PREFIX}{workspace_id}"
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
