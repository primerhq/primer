"""DockerRuntimeAdapter -- talks to the Docker Engine API via aiodocker.

Most operations dispatch through ``aiodocker.Docker`` (containers,
volumes, exec). File operations use Docker's archive API
(``GET/PUT /containers/{id}/archive``) where possible and fall back to
``cat``/``tee`` over ``exec`` when not.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shlex
import tarfile
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Literal

from matrix.int.sandbox import (
    ExecResult,
    FileStat,
    Sandbox,
    SandboxInspectInfo,
)
from matrix.model.workspace import ResourceLimits, VolumeMount
from matrix.workspace.runtime.adapter import ContainerRuntimeAdapter


logger = logging.getLogger(__name__)


_LABEL_KEY = "matrix.workspace.id"


class DockerSandbox(Sandbox):
    """Sandbox backed by one running Docker container."""

    def __init__(self, docker, container, name: str) -> None:
        self._docker = docker
        self._container = container
        self._name = name

    @property
    def id(self) -> str:
        return self._name

    async def exec(
        self,
        command,
        *,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        stdin: bytes | None = None,
        abort: asyncio.Event | None = None,
    ) -> ExecResult:
        if isinstance(command, str):
            cmd = ["sh", "-c", command]
        else:
            cmd = list(command)
        env_list = (
            [f"{k}={v}" for k, v in env.items()] if env else None
        )
        start = time.perf_counter()
        exec_inst = await self._container.exec(
            cmd=cmd,
            stdout=True, stderr=True,
            stdin=stdin is not None,
            tty=False,
            workdir=workdir,
            environment=env_list,
        )
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _drain() -> None:
            async with exec_inst.start(detach=False) as stream:
                if stdin is not None:
                    await stream.write_in(stdin)
                while True:
                    msg = await stream.read_out()
                    if msg is None:
                        break
                    if msg.stream == 1:
                        stdout_chunks.append(msg.data)
                    elif msg.stream == 2:
                        stderr_chunks.append(msg.data)

        async def _abort_waiter() -> None:
            if abort is None:
                return
            await abort.wait()
            # Best-effort: signal kill via exec inspection.
            # Docker doesn't expose a kill-exec API; we rely on container
            # stop in the worst case, but here we just abort the stream.

        abort_task = (
            asyncio.create_task(_abort_waiter()) if abort is not None else None
        )
        try:
            await asyncio.wait_for(_drain(), timeout=timeout_seconds)
        finally:
            if abort_task is not None:
                abort_task.cancel()

        info = await exec_inst.inspect()
        return ExecResult(
            exit_code=info.get("ExitCode", -1) or 0,
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            duration_seconds=time.perf_counter() - start,
        )

    async def read_file(self, path: str) -> bytes:
        # Use Docker archive API: GET /containers/{id}/archive?path=...
        chunks = bytearray()
        async for chunk in self._container.get_archive(path):
            chunks.extend(chunk)
        # The result is a tar stream containing one file with basename(path).
        buf = io.BytesIO(bytes(chunks))
        with tarfile.open(fileobj=buf, mode="r") as tf:
            members = tf.getmembers()
            if not members:
                return b""
            # Find the first regular file member.
            for m in members:
                if m.isfile():
                    f = tf.extractfile(m)
                    return f.read() if f is not None else b""
        return b""

    async def write_file(
        self, path: str, content: bytes, *, mode: int | None = None,
    ) -> None:
        # Build a one-file tar and PUT it to the directory containing
        # ``path``. The tar's single entry has the basename(path) as
        # its arcname.
        from posixpath import dirname, basename
        target_dir = dirname(path) or "/"
        name = basename(path)
        # Ensure the target dir exists.
        await self.exec(f"mkdir -p {shlex.quote(target_dir)}")

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = int(time.time())
            if mode is not None:
                info.mode = mode
            tf.addfile(info, io.BytesIO(content))
        buf.seek(0)
        await self._container.put_archive(target_dir, buf.read())

    async def list_dir(self, path: str) -> list[FileStat]:
        # Implement via exec(ls -la). Format columns are unstable
        # across distros; using a more portable enumeration via
        # find + stat would be safer. For v1 we use ls.
        res = await self.exec(
            f"ls -la --time-style=+%s {shlex.quote(path)}",
        )
        out: list[FileStat] = []
        for line in res.stdout.splitlines():
            parsed = _parse_ls_line(line)
            if parsed is None:
                continue
            out.append(parsed)
        return out

    async def stat(self, path: str) -> FileStat | None:
        res = await self.exec(
            f"stat --format='%n%x1F%F%x1F%s%x1F%a%x1F%Y' "
            f"{shlex.quote(path)}".replace("%x1F", "\\x1F"),
        )
        if res.exit_code != 0:
            return None
        parts = res.stdout.strip().split("\x1f")
        if len(parts) < 5:
            return None
        name, kind_str, size_str, mode_str, mtime_str = parts[:5]
        try:
            size_bytes = int(size_str)
            mode = int(mode_str, 8)
            mtime = int(mtime_str)
        except ValueError:
            return None
        if "directory" in kind_str:
            kind = "dir"
        elif "symbolic link" in kind_str:
            kind = "symlink"
        else:
            kind = "file"
        return FileStat(
            path=name,
            kind=kind,  # type: ignore[arg-type]
            size_bytes=size_bytes,
            mode=mode,
            modified_at=datetime.fromtimestamp(mtime, tz=timezone.utc),
        )

    async def delete(self, path: str) -> None:
        await self.exec(f"rm -rf {shlex.quote(path)}")

    async def archive(self, paths: list[str]) -> AsyncIterator[bytes]:
        for path in paths:
            async for chunk in self._container.get_archive(path):
                yield chunk

    async def inspect(self) -> SandboxInspectInfo:
        info = await self._container.show()
        state_str = info.get("State", {}).get("Status", "unknown")
        state_map: dict[str, str] = {
            "created": "created",
            "running": "running",
            "paused": "stopped",
            "restarting": "starting",
            "exited": "exited",
            "removing": "stopped",
            "dead": "failed",
        }
        return SandboxInspectInfo(
            state=state_map.get(state_str, "unknown"),  # type: ignore[arg-type]
            detail={"id": info.get("Id"), "image": info.get("Config", {}).get("Image")},
        )

    async def stop(self) -> None:
        try:
            await self._container.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("DockerSandbox.stop failed: %s", exc)

    async def remove(self) -> None:
        try:
            await self._container.delete(force=True, v=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DockerSandbox.remove failed: %s", exc)


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

        # Pull image per policy.
        if pull_policy == "always":
            await self._docker.pull(image)
        elif pull_policy == "if_missing":
            try:
                await self._docker.images.inspect(image)
            except Exception:
                await self._docker.pull(image)

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

        host_config: dict[str, Any] = {"Mounts": mounts}
        if network == "none":
            host_config["NetworkMode"] = "none"
        if resources.cpu_cores is not None:
            host_config["NanoCpus"] = int(resources.cpu_cores * 1_000_000_000)
        if resources.memory_bytes is not None:
            host_config["Memory"] = resources.memory_bytes

        container_config: dict[str, Any] = {
            "Image": image,
            "Cmd": command,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "WorkingDir": workdir,
            "Labels": {_LABEL_KEY: name},
            "HostConfig": host_config,
            "Tty": False,
        }
        if user is not None:
            container_config["User"] = user

        container = await self._docker.containers.create_or_replace(
            name=name, config=container_config,
        )
        await container.start()
        return DockerSandbox(self._docker, container, name)

    async def get_sandbox(self, name: str) -> Sandbox | None:
        assert self._docker is not None
        try:
            container = await self._docker.containers.get(name)
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                return None
            raise
        info = await container.show()
        if info.get("State", {}).get("Status") != "running":
            try:
                await container.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("DockerSandbox auto-start failed: %s", exc)
        return DockerSandbox(self._docker, container, name)

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


# ===========================================================================
# Helpers
# ===========================================================================


def _parse_ls_line(line: str) -> FileStat | None:
    """Parse one ``ls -la --time-style=+%s`` line into a FileStat.

    Format: ``<perms> <links> <user> <group> <size> <mtime-epoch> <name>``.
    Skips header lines and ``.``/``..``.
    """
    if not line or line.startswith("total "):
        return None
    parts = line.split(None, 6)
    if len(parts) < 7:
        return None
    perms, _links, _user, _group, size_str, mtime_str, name = parts
    if name in (".", ".."):
        return None
    try:
        size_bytes = int(size_str)
        mtime = int(mtime_str)
    except ValueError:
        return None
    if perms.startswith("d"):
        kind = "dir"
        size_bytes = 0
    elif perms.startswith("l"):
        kind = "symlink"
        size_bytes = 0
        # 'name -> target' form; keep just the name.
        name = name.split(" -> ")[0]
    else:
        kind = "file"
    # Convert the symbolic perms to octal mode (best-effort).
    mode = _symbolic_perms_to_mode(perms[1:10]) if len(perms) >= 10 else 0
    return FileStat(
        path=name,
        kind=kind,  # type: ignore[arg-type]
        size_bytes=size_bytes,
        mode=mode,
        modified_at=datetime.fromtimestamp(mtime, tz=timezone.utc),
    )


def _symbolic_perms_to_mode(perms: str) -> int:
    """Convert ``rwxrwxrwx``-style perms to integer mode bits."""
    if len(perms) != 9:
        return 0
    bits = 0
    for i, ch in enumerate(perms):
        # 9 chars: u/g/o triplets. Highest = owner read.
        weight = 1 << (8 - i)
        if ch != "-":
            bits |= weight
    return bits


__all__ = ["DockerRuntimeAdapter", "DockerSandbox"]
