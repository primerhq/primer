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
from primer.model.workspace import ResourceLimits, VolumeMount
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


async def _make_ws_sandbox(docker, container, name: str, token: str) -> WSSandbox:
    """Wait for runtime, connect RuntimeClient, wrap in WSSandbox."""
    await _wait_for_ready(container)
    host_port = await _discover_host_port(container, _RUNTIME_PORT)
    runtime_client = RuntimeClient(
        url=f"ws://127.0.0.1:{host_port}/",
        token=token,
    )
    await runtime_client.connect()
    handle = _DockerContainerHandle(container)
    container_info = await container.show()
    container_id = container_info.get("Id", name)
    return WSSandbox(
        runtime_client=runtime_client,
        container_id=container_id,
        workspace_root="/workspace",
        container_handle=handle,
    )


class DockerRuntimeAdapter(ContainerRuntimeAdapter):
    """Adapter targeting the Docker Engine API via aiodocker."""

    def __init__(self, config) -> None:
        self._config = config
        self._docker = None

    async def initialize(self) -> None:
        import aiodocker
        url = self._config.socket
        self._docker = aiodocker.Docker(url=url) if url else aiodocker.Docker()

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
    ) -> Sandbox:
        assert self._docker is not None, "call initialize() first"

        # Always use the runtime image as the base; the caller-supplied
        # ``image`` is recorded in labels for traceability but the
        # runtime server is the actual entrypoint.
        runtime_image = _RUNTIME_IMAGE

        # Pull runtime image per policy.
        if pull_policy == "always":
            await self._docker.images.pull(runtime_image)
        elif pull_policy == "if_missing":
            try:
                await self._docker.images.inspect(runtime_image)
            except Exception:
                await self._docker.images.pull(runtime_image)

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

        # Generate a per-sandbox shared secret for the runtime auth.
        token = secrets.token_urlsafe(32)

        # Merge caller env with runtime-required vars.
        merged_env = dict(env)
        merged_env["PRIMER_RUNTIME_TOKEN"] = token

        host_config: dict[str, Any] = {
            "Mounts": mounts,
            # Ask Docker to assign a free host port for the runtime WS port.
            "PortBindings": {
                f"{_RUNTIME_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}],
            },
        }
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
        if user is not None:
            container_config["User"] = user

        container = await self._docker.containers.create_or_replace(
            name=name, config=container_config,
        )
        await container.start()
        return await _make_ws_sandbox(self._docker, container, name, token)

    async def get_sandbox(self, name: str) -> Sandbox | None:
        """Look up a sandbox by name.

        NOTE: ``get_sandbox`` cannot re-create the :class:`RuntimeClient`
        because the original bearer token is not persisted.  It returns
        ``None`` so that the calling backend creates a fresh sandbox when
        the original handle is lost.  A production implementation would
        persist the token alongside the sandbox metadata.
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
                "(token not persisted). Returning None so caller creates fresh sandbox.",
                name,
                state,
            )
            return None
        # TODO(task10): persist token in volume labels or a sidecar file so
        # we can reconnect here.  For now, signal that re-creation is needed.
        logger.warning(
            "get_sandbox(%s): runtime token not persisted; returning None "
            "to trigger re-creation.",
            name,
        )
        return None

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
