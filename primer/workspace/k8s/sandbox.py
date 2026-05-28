"""K8sSandbox -- :class:`Sandbox` backed by one StatefulSet Pod.

Speaks the K8s API via ``kubernetes-asyncio`` directly (no
``ContainerRuntimeAdapter`` layer — K8s is its own world). All exec
work goes through the K8s exec stream; file ops use tar-over-exec
(the ``kubectl cp`` pattern).
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

from primer.int.sandbox import (
    ExecResult,
    FileStat,
    Sandbox,
    SandboxInspectInfo,
)


logger = logging.getLogger(__name__)


_POD_PHASE_TO_STATE: dict[str, str] = {
    "Pending": "starting",
    "Running": "running",
    "Succeeded": "exited",
    "Failed": "failed",
    "Unknown": "unknown",
}


class K8sSandbox(Sandbox):
    """Sandbox backed by one K8s Pod (via its StatefulSet).

    Constructed by :class:`KubernetesWorkspaceBackend`; do not
    instantiate directly outside the backend or for tests.
    """

    def __init__(
        self,
        *,
        core_v1,
        apps_v1,
        ws_api,
        namespace: str,
        sts_name: str,
        pod_name: str,
        sandbox_id: str,
        pvc_name: str,
    ) -> None:
        self._core_v1 = core_v1
        self._apps_v1 = apps_v1
        self._ws_api = ws_api
        self._namespace = namespace
        self._sts_name = sts_name
        self._pod_name = pod_name
        self._id = sandbox_id
        self._pvc_name = pvc_name

    @property
    def id(self) -> str:
        return self._id

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
            cmd = ["sh", "-c", f"cd {shlex.quote(workdir)} && {command}"]
        else:
            cmd = ["sh", "-c", f"cd {shlex.quote(workdir)} && " + " ".join(shlex.quote(c) for c in command)]
        # kubernetes-asyncio's exec stream API; the ws_api is a
        # WsApiClient that supports connect_get_namespaced_pod_exec.
        start = time.perf_counter()
        from kubernetes_asyncio.stream import WsApiClient

        client = self._ws_api if self._ws_api is not None else WsApiClient()
        from kubernetes_asyncio.client import CoreV1Api

        core = CoreV1Api(client)
        stream = await core.connect_get_namespaced_pod_exec(
            self._pod_name, self._namespace,
            command=cmd,
            stderr=True, stdin=stdin is not None,
            stdout=True, tty=False,
        )
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _drain() -> None:
            if stdin is not None:
                await stream.write_stdin(stdin.decode("utf-8", errors="replace"))
            while True:
                msg = await stream.receive()
                if msg.type in ("close", "closed"):
                    break
                data = msg.data if isinstance(msg.data, bytes) else msg.data.encode("utf-8")
                if not data:
                    continue
                channel = data[0]
                payload = data[1:]
                if channel == 1:
                    stdout_chunks.append(payload)
                elif channel == 2:
                    stderr_chunks.append(payload)

        try:
            await asyncio.wait_for(_drain(), timeout=timeout_seconds)
        finally:
            try:
                await stream.close()
            except Exception:
                pass

        return ExecResult(
            exit_code=0,  # k8s exec doesn't expose exit code via stream
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            duration_seconds=time.perf_counter() - start,
        )

    async def read_file(self, path: str) -> bytes:
        # tar-over-exec: dispatch `tar c <path>` and read stdout as tar.
        res = await self.exec(
            ["sh", "-c", f"tar cf - {shlex.quote(path)}"],
        )
        buf = io.BytesIO(res.stdout.encode("utf-8", errors="replace"))
        with tarfile.open(fileobj=buf, mode="r") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    f = tf.extractfile(m)
                    return f.read() if f is not None else b""
        return b""

    async def write_file(
        self, path: str, content: bytes, *, mode: int | None = None,
    ) -> None:
        from posixpath import basename, dirname

        target_dir = dirname(path) or "/"
        name = basename(path)
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
        await self.exec(
            ["sh", "-c", f"tar xf - -C {shlex.quote(target_dir)}"],
            stdin=buf.read(),
        )

    async def list_dir(self, path: str) -> list[FileStat]:
        from primer.workspace.runtime.docker import _parse_ls_line  # reuse parser

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
            f"stat --format='%n\\t%F\\t%s\\t%a\\t%Y' {shlex.quote(path)}",
        )
        if res.exit_code != 0 or not res.stdout.strip():
            return None
        parts = res.stdout.strip().split("\t")
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
        joined = " ".join(shlex.quote(p) for p in paths)
        res = await self.exec(["sh", "-c", f"tar cf - {joined}"])
        yield res.stdout.encode("utf-8", errors="replace")

    async def inspect(self) -> SandboxInspectInfo:
        pod = await self._core_v1.read_namespaced_pod(
            self._pod_name, self._namespace,
        )
        phase = pod.status.phase if pod.status else "Unknown"
        return SandboxInspectInfo(
            state=_POD_PHASE_TO_STATE.get(phase, "unknown"),  # type: ignore[arg-type]
            detail={"phase": phase, "pod": self._pod_name},
        )

    async def stop(self) -> None:
        """Scale the StatefulSet to 0 replicas (terminates the Pod;
        PVC preserved). The handle stays usable -- a subsequent exec
        will fail until ``KubernetesWorkspaceBackend.get`` scales back."""
        body = {"spec": {"replicas": 0}}
        try:
            await self._apps_v1.patch_namespaced_stateful_set_scale(
                self._sts_name, self._namespace, body,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("K8sSandbox.stop scale failed: %s", exc)

    async def remove(self) -> None:
        """Delete the StatefulSet AND its PVC."""
        try:
            await self._apps_v1.delete_namespaced_stateful_set(
                self._sts_name, self._namespace,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete StatefulSet failed: %s", exc)
        try:
            await self._core_v1.delete_namespaced_persistent_volume_claim(
                self._pvc_name, self._namespace,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete PVC failed: %s", exc)


__all__ = ["K8sSandbox"]
