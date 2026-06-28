"""DockerRuntimeAdapter -- talks to the Docker Engine API via aiodocker.

``create_sandbox`` and ``get_sandbox`` now provision the workspace runtime
image (``primer/workspace-runtime:1.0``) and return a :class:`WSSandbox`
backed by a :class:`RuntimeClient`.  The old ``DockerSandbox`` class has
been removed.

Container lifecycle (stop / remove) is delegated to a lightweight
:class:`_DockerContainerHandle` that wraps the aiodocker container object.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Literal

from primer.int.sandbox import Sandbox
from primer.model.except_ import ConfigError
from primer.model.workspace import (
    ContainerReachabilityBridge,
    ContainerReachabilityConfig,
    ContainerReachabilityHostPort,
    ResourceLimits,
    VolumeMount,
)
from primer.workspace.runtime.adapter import ContainerRuntimeAdapter
from primer.workspace.runtime.runtime_client import RuntimeClient
from primer.workspace.runtime.ws_sandbox import WSSandbox


logger = logging.getLogger(__name__)


_LABEL_KEY = "primer.workspace.id"
_RUNTIME_IMAGE = "primer/workspace-runtime:1.0"
_RUNTIME_PORT = 5959
_READY_POLL_INTERVAL_S = 0.1
_READY_TIMEOUT_S = 30.0


class _DockerContainerHandle:
    """Implements :class:`ContainerHandle` for an aiodocker container object."""

    def __init__(self, container) -> None:
        self._container = container

    async def stop(self) -> None:
        try:
            await self._container.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("_DockerContainerHandle.stop failed: %s", exc)

    async def remove(self) -> None:
        try:
            await self._container.delete(force=True, v=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_DockerContainerHandle.remove failed: %s", exc)


# Verify _DockerContainerHandle satisfies the ContainerHandle protocol at import
# time by using isinstance check once an instance is created; protocol
# conformance is structural so the assert is best placed in tests.


async def _wait_for_ready(container, *, timeout_s: float = _READY_TIMEOUT_S) -> None:
    """Poll /workspace/.runtime.ready inside the container via docker exec.

    Retries every ``_READY_POLL_INTERVAL_S`` seconds.  Raises
    :class:`TimeoutError` if the file does not appear within *timeout_s*.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        try:
            exec_inst = await container.exec(
                cmd=["cat", "/workspace/.runtime.ready"],
                stdout=True,
                stderr=False,
                stdin=False,
                tty=False,
            )
            output_chunks: list[bytes] = []
            async with exec_inst.start(detach=False) as stream:
                while True:
                    msg = await stream.read_out()
                    if msg is None:
                        break
                    if msg.stream == 1:
                        output_chunks.append(msg.data)
            inspect_info = await exec_inst.inspect()
            if inspect_info.get("ExitCode", 1) == 0:
                return
        except Exception:  # noqa: BLE001
            pass
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Runtime did not become ready within {timeout_s}s"
            )
        await asyncio.sleep(_READY_POLL_INTERVAL_S)


def _token_from_inspect(info: dict) -> str | None:
    """Recover the runtime bearer token from a ``docker inspect`` payload.

    ``create_sandbox`` injects the token into the container env as
    ``PRIMER_RUNTIME_TOKEN`` (canonical, read by the runtime server) and
    ``RUNTIME_TOKEN`` (operator-facing alias). The inspect payload exposes
    the env as a list of ``"KEY=VALUE"`` strings under ``Config.Env``.
    Returns the first key that is present, or ``None`` if neither is.
    """
    env_list = (info.get("Config", {}) or {}).get("Env", []) or []
    found: dict[str, str] = {}
    for entry in env_list:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        if key in ("PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"):
            found[key] = value
    # Prefer the canonical key the runtime reads.
    for key in ("PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"):
        if key in found:
            return found[key]
    return None


async def _discover_host_port(container, container_port: int) -> int:
    """Return the host port mapped to *container_port* via docker inspect."""
    info = await container.show()
    ports = (
        info.get("NetworkSettings", {})
        .get("Ports", {})
        .get(f"{container_port}/tcp", [])
    ) or []
    for mapping in ports:
        if mapping and mapping.get("HostPort"):
            return int(mapping["HostPort"])
    raise RuntimeError(
        f"No host port mapping found for container port {container_port}"
    )


async def _make_ws_sandbox(
    docker,
    container,
    name: str,
    token: str,
    *,
    reachability: ContainerReachabilityConfig,
) -> WSSandbox:
    """Wait for runtime, connect RuntimeClient, wrap in WSSandbox.

    The RuntimeClient URL depends on ``reachability``:

    * ``host_port`` -- discover the host port docker mapped 5959 to and
      use it together with the configured ``bind_host`` over loopback /
      LAN. The discovered port is also stashed on the returned
      :class:`WSSandbox` as ``mapped_host_port`` so the backend can
      record it on the workspace.
    * ``bridge_network`` -- reach the runtime through docker's bridge
      DNS at ``workspace-<id>:5959``. The container name is the docker
      hostname, so we use it verbatim.
    """
    await _wait_for_ready(container)
    mapped_host_port: int | None = None
    if isinstance(reachability, ContainerReachabilityHostPort):
        mapped_host_port = await _discover_host_port(container, _RUNTIME_PORT)
        url = f"ws://{reachability.bind_host}:{mapped_host_port}/"
    elif isinstance(reachability, ContainerReachabilityBridge):
        url = f"ws://{name}:{_RUNTIME_PORT}/"
    else:  # pragma: no cover -- discriminated union exhausted
        raise ValueError(
            f"Unknown container reachability kind: {reachability.kind!r}"
        )
    runtime_client = RuntimeClient(url=url, token=token)
    await runtime_client.connect()
    handle = _DockerContainerHandle(container)
    container_info = await container.show()
    container_id = container_info.get("Id", name)
    sandbox = WSSandbox(
        runtime_client=runtime_client,
        container_id=container_id,
        workspace_root="/workspace",
        container_handle=handle,
    )
    # Stash the mapped host port on the sandbox so the backend can pass
    # it back into ``build_runtime_url`` (only meaningful in host_port
    # mode; remains None for bridge_network).
    sandbox.mapped_host_port = mapped_host_port  # type: ignore[attr-defined]
    return sandbox


class DockerRuntimeAdapter(ContainerRuntimeAdapter):
    """Adapter targeting the Docker Engine API via aiodocker."""

    def __init__(self, config) -> None:
        self._config = config
        self._docker = None

    async def initialize(self) -> None:
        import aiodocker
        # ``self._config`` is either the legacy DockerRuntimeConfig (which
        # exposes ``.socket``) or a :class:`ContainerWorkspaceConfig` with a
        # ``.connection`` block. Handle both so the adapter is usable from
        # the redesigned backend without forcing every existing caller to
        # migrate at once.
        url = self._extract_docker_url()
        self._docker = aiodocker.Docker(url=url) if url else aiodocker.Docker()

    def _extract_docker_url(self) -> str | None:
        """Resolve the Docker engine URL from whichever config shape we
        were handed."""
        cfg = self._config
        socket = getattr(cfg, "socket", None)
        if socket is not None:
            return socket
        conn = getattr(cfg, "connection", None)
        if conn is None:
            return None
        if getattr(conn, "kind", None) == "socket":
            sp = getattr(conn, "socket_path", None)
            return f"unix://{sp}" if sp else None
        if getattr(conn, "kind", None) == "remote":
            return getattr(conn, "url", None)
        return None

    async def aclose(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None

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
        reachability: ContainerReachabilityConfig | None = None,
        token: str | None = None,
    ) -> Sandbox:
        assert self._docker is not None, "call initialize() first"

        # Always use the runtime image as the base; the caller-supplied
        # ``image`` is recorded in labels for traceability but the
        # runtime server is the actual entrypoint.
        runtime_image = _RUNTIME_IMAGE

        # Backwards-compat: legacy callers (the live docker integration
        # tests) don't pass reachability/token. Default to host_port on
        # loopback and a fresh token so they keep working.
        if reachability is None:
            reachability = ContainerReachabilityHostPort()
        if token is None:
            token = secrets.token_urlsafe(32)

        # Pull runtime image per policy. A pull failure — the image is absent
        # from every registry (e.g. a local-only name that was never built) —
        # is a provisioning/config problem, not an internal error. Surface it
        # as a ConfigError (503 service-unavailable) instead of letting the raw
        # aiodocker DockerError escape the request as an unhandled 500.
        from aiodocker import DockerError
        try:
            if pull_policy == "always":
                await self._docker.images.pull(runtime_image)
            elif pull_policy == "if_missing":
                try:
                    await self._docker.images.inspect(runtime_image)
                except Exception:
                    await self._docker.images.pull(runtime_image)
        except DockerError as exc:
            raise ConfigError(
                f"container runtime image {runtime_image!r} is unavailable "
                f"({exc}); the workspace cannot be provisioned"
            ) from exc

        # Create the named volume.
        try:
            await self._docker.volumes.create({"Name": volume_name})
        except Exception as exc:  # noqa: BLE001 -- volume may exist
            logger.debug("volume create returned %s (likely exists)", exc)

        mounts = [
            {
                "Type": "volume",
                "Source": volume_name,
                "Target": volume_target,
                "ReadOnly": False,
            },
        ]
        for vm in extra_mounts:
            mounts.append({
                "Type": "bind",
                "Source": vm.source,
                "Target": vm.target,
                "ReadOnly": vm.read_only,
            })

        # Merge caller env with runtime-required vars.
        merged_env = dict(env)
        # Runtime container reads ``PRIMER_RUNTIME_TOKEN``; ``RUNTIME_TOKEN``
        # is the operator-facing name used by the K8s Secret. Inject both
        # so both startup paths work without operator-side adjustment.
        merged_env["PRIMER_RUNTIME_TOKEN"] = token
        merged_env["RUNTIME_TOKEN"] = token

        host_config: dict[str, Any] = {
            "Mounts": mounts,
        }
        endpoints: dict[str, Any] | None = None
        if isinstance(reachability, ContainerReachabilityHostPort):
            host_config["PortBindings"] = {
                f"{_RUNTIME_PORT}/tcp": [
                    {"HostIp": reachability.bind_host, "HostPort": ""},
                ],
            }
        elif isinstance(reachability, ContainerReachabilityBridge):
            # Join the shared bridge network so the platform container
            # can resolve ``workspace-<id>`` via docker's embedded DNS.
            host_config["NetworkMode"] = reachability.network_name
            endpoints = {
                reachability.network_name: {
                    # ``Aliases`` so the platform can also reach the
                    # workspace by an alternate name if it wants to.
                    "Aliases": [name],
                },
            }
        else:  # pragma: no cover
            raise ValueError(
                f"Unknown container reachability kind: {reachability.kind!r}"
            )

        if network == "none":
            # "none" network mode is incompatible with port publishing.
            # We need the runtime accessible over loopback, so we do NOT
            # set NetworkMode=none; skip the restriction instead.
            # TODO(task10): revisit network isolation for production.
            pass
        if resources.cpu_cores is not None:
            host_config["NanoCpus"] = int(resources.cpu_cores * 1_000_000_000)
        if resources.memory_bytes is not None:
            host_config["Memory"] = resources.memory_bytes

        container_config: dict[str, Any] = {
            "Image": runtime_image,
            "Env": [f"{k}={v}" for k, v in merged_env.items()],
            "WorkingDir": workdir,
            "Labels": {
                _LABEL_KEY: name,
                "primer.workspace.template.image": image,
            },
            "HostConfig": host_config,
            "ExposedPorts": {f"{_RUNTIME_PORT}/tcp": {}},
            "Tty": False,
        }
        if endpoints is not None:
            container_config["NetworkingConfig"] = {
                "EndpointsConfig": endpoints,
            }
        if user is not None:
            container_config["User"] = user

        container = await self._docker.containers.create_or_replace(
            name=name, config=container_config,
        )
        await container.start()
        return await _make_ws_sandbox(
            self._docker, container, name, token, reachability=reachability,
        )

    async def get_sandbox(self, name: str) -> Sandbox | None:
        """Re-attach to a running workspace container by name.

        Rehydrates the live :class:`WSSandbox` for a container that this
        process did not start (the API/worker split) or that survived a
        platform restart. The bearer token is NOT held in process memory
        across the split, but it *is* persisted: ``create_sandbox`` injects
        it into the container's environment as ``PRIMER_RUNTIME_TOKEN``
        (and the ``RUNTIME_TOKEN`` alias), so we recover it from
        ``docker inspect`` (``Config.Env``) and reconnect the
        :class:`RuntimeClient` against the same URL the create path built.

        Returns ``None`` when:

        * the container does not exist (``404``);
        * the container is not ``running`` (a stopped/exited container has
          no runtime listening to reconnect to);
        * the token cannot be recovered from the container env.

        This mirrors how :class:`KubernetesWorkspaceBackend` recovers the
        token from the per-workspace Secret on re-attach.
        """
        assert self._docker is not None
        try:
            container = await self._docker.containers.get(name)
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                return None
            raise
        info = await container.show()
        state = info.get("State", {}).get("Status")
        if state != "running":
            logger.warning(
                "get_sandbox(%s): container is %s; cannot reconnect runtime "
                "to a non-running container. Returning None.",
                name,
                state,
            )
            return None
        token = _token_from_inspect(info)
        if token is None:
            logger.warning(
                "get_sandbox(%s): PRIMER_RUNTIME_TOKEN not found in container "
                "env; cannot reconnect runtime. Returning None.",
                name,
            )
            return None
        reachability = self._reachability_for_reattach()
        try:
            sandbox = await _make_ws_sandbox(
                self._docker, container, name, token,
                reachability=reachability,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_sandbox(%s): runtime reconnect failed: %s", name, exc,
            )
            return None
        # Stash the recovered token so the backend can populate the
        # workspace ``runtime_meta`` on re-attach (mirrors how the K8s
        # backend recovers it from the Secret). Parallels the
        # ``mapped_host_port`` stash in ``_make_ws_sandbox``.
        sandbox.recovered_token = token  # type: ignore[attr-defined]
        return sandbox

    def _reachability_for_reattach(self) -> ContainerReachabilityConfig:
        """Resolve the reachability config the running container was
        started with so :func:`_make_ws_sandbox` rebuilds the same URL.

        The provider config carries it on ``.reachability``; legacy
        configs (the live docker integration tests) omit it, in which case
        we default to ``host_port`` on loopback -- the same fallback
        :meth:`create_sandbox` uses."""
        reachability = getattr(self._config, "reachability", None)
        if reachability is None:
            return ContainerReachabilityHostPort()
        return reachability

    async def list_sandboxes(self) -> list[str]:
        assert self._docker is not None
        containers = await self._docker.containers.list(
            all=True, filters={"label": [_LABEL_KEY]},
        )
        names: list[str] = []
        for c in containers:
            for raw in c._container.get("Names", []) or []:
                names.append(raw.lstrip("/"))
        return names

    async def remove_volume(self, name: str) -> None:
        assert self._docker is not None
        try:
            vol = await self._docker.volumes.get(name)
            await vol.delete()
        except Exception as exc:  # noqa: BLE001
            if "404" not in str(exc):
                raise


__all__ = ["DockerRuntimeAdapter"]
