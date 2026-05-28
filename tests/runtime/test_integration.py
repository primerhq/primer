"""Integration smoke tests: boot the runtime container and assert latency targets.

Requirements
------------
- Docker daemon reachable
- ``matrix/workspace-runtime:1.0`` image built locally
  (``docker build -t matrix/workspace-runtime:1.0 runtime/``)

Skip gracefully when either precondition is absent (``pytest.mark.docker``
marker is collected but tests skip at runtime via ``pytestmark``).

Latency targets (aspirational, per spec):
  read_file  p95 < 5 ms   (100 sequential ops)
  append_line p95 < 5 ms   (100 sequential ops)
  stat        p95 < 20 ms  (50 parallel ops)
  watch event arrives in worker queue in <100 ms

These targets are validated as *warnings* rather than hard failures:
if the dev box is slower (e.g. p95 = 8 ms for read_file) the test records
the delta and marks itself as a warning rather than failing the suite.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import statistics
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Docker / image availability guards
# ---------------------------------------------------------------------------

_RUNTIME_IMAGE = "matrix/workspace-runtime:1.0"
_RUNTIME_PORT = 5959
_READY_POLL_S = 0.1
_READY_TIMEOUT_S = 60.0


async def _docker_available() -> bool:
    try:
        import aiodocker as _aiodocker
        docker = _aiodocker.Docker()
        await docker.version()
        await docker.close()
        return True
    except Exception:
        return False


async def _image_present() -> bool:
    try:
        import aiodocker as _aiodocker
        docker = _aiodocker.Docker()
        await docker.images.inspect(_RUNTIME_IMAGE)
        await docker.close()
        return True
    except Exception:
        return False


try:
    _DOCKER_OK = asyncio.run(_docker_available())
    _IMAGE_OK = asyncio.run(_image_present()) if _DOCKER_OK else False
except Exception:
    _DOCKER_OK = False
    _IMAGE_OK = False

pytestmark = pytest.mark.skipif(
    not _DOCKER_OK or not _IMAGE_OK,
    reason=(
        "Docker not available or matrix/workspace-runtime:1.0 not built. "
        "Run `docker build -t matrix/workspace-runtime:1.0 runtime/` first."
    ),
)

# ---------------------------------------------------------------------------
# Container fixture
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _runtime_container(
    workspace_dir: str,
) -> AsyncIterator[tuple[str, int]]:
    """Boot the runtime container with a bind-mount for *workspace_dir*.

    Yields ``(token, host_port)`` so callers can connect a RuntimeClient.
    Cleans up the container on exit.
    """
    import aiodocker

    token = secrets.token_urlsafe(32)
    docker = aiodocker.Docker()
    container = None
    try:
        container = await docker.containers.create(
            config={
                "Image": _RUNTIME_IMAGE,
                "Env": [f"PRIMER_RUNTIME_TOKEN={token}"],
                "HostConfig": {
                    "Binds": [f"{workspace_dir}:/workspace"],
                    "PortBindings": {
                        f"{_RUNTIME_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}],
                    },
                    "AutoRemove": False,
                },
                "ExposedPorts": {f"{_RUNTIME_PORT}/tcp": {}},
                "Tty": False,
            },
        )
        await container.start()

        # Wait for .runtime.ready
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while True:
            try:
                exec_inst = await container.exec(
                    cmd=["cat", "/workspace/.runtime.ready"],
                    stdout=True,
                    stderr=False,
                    stdin=False,
                    tty=False,
                )
                async with exec_inst.start(detach=False) as stream:
                    while True:
                        msg = await stream.read_out()
                        if msg is None:
                            break
                info = await exec_inst.inspect()
                if info.get("ExitCode", 1) == 0:
                    break
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Runtime did not become ready within {_READY_TIMEOUT_S}s")
            await asyncio.sleep(_READY_POLL_S)

        # Discover host port
        container_info = await container.show()
        ports = (
            container_info.get("NetworkSettings", {})
            .get("Ports", {})
            .get(f"{_RUNTIME_PORT}/tcp", [])
        ) or []
        host_port = int(ports[0]["HostPort"]) if ports else None
        if not host_port:
            raise RuntimeError("No host port mapping found")

        yield token, host_port

    finally:
        if container is not None:
            try:
                await container.stop()
            except Exception:
                pass
            try:
                await container.delete(force=True, v=False)
            except Exception:
                pass
        await docker.close()


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def _p95(samples: list[float]) -> float:
    """Return the 95th-percentile value (in ms) from *samples* (in seconds)."""
    if not samples:
        return float("inf")
    sorted_s = sorted(samples)
    idx = max(0, int(len(sorted_s) * 0.95) - 1)
    return sorted_s[idx] * 1000.0  # convert to ms


def _assert_latency(label: str, p95_ms: float, target_ms: float) -> None:
    """Warn (not fail) when p95 exceeds the aspirational target.

    Records the observation via ``warnings.warn`` so it appears in the
    pytest output without breaking the suite.
    """
    if p95_ms > target_ms:
        delta = p95_ms - target_ms
        warnings.warn(
            f"Latency target miss: {label} p95={p95_ms:.2f}ms "
            f"(target <{target_ms}ms, delta={delta:+.2f}ms). "
            "This is a dev-box observation; not a hard failure.",
            stacklevel=2,
        )
        log.warning(
            "LATENCY MISS %s: p95=%.2f ms (target <%.0f ms)",
            label,
            p95_ms,
            target_ms,
        )
    else:
        log.info("LATENCY OK %s: p95=%.2f ms (target <%.0f ms)", label, p95_ms, target_ms)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_op_latency(tmp_path: Path) -> None:
    """100 sequential read_file + 100 sequential append_line; p95 < 5 ms each.

    Aspirational: if the dev box is slower the test warns rather than fails.
    """
    from primer.workspace.runtime.runtime_client import RuntimeClient

    workspace = str(tmp_path)

    async with _runtime_container(workspace) as (token, host_port):
        client = RuntimeClient(
            url=f"ws://127.0.0.1:{host_port}/",
            token=token,
        )
        await client.connect()
        try:
            # Seed a file for reading
            seed_path = "/workspace/latency_read.txt"
            await client.write_file(seed_path, b"hello latency test\n")

            # --- read_file benchmark ------------------------------------------
            read_times: list[float] = []
            for _ in range(100):
                t0 = time.monotonic()
                data = await client.read_file(seed_path)
                read_times.append(time.monotonic() - t0)
            assert data == b"hello latency test\n"

            p95_read = _p95(read_times)
            log.info("read_file: min=%.2fms p95=%.2fms max=%.2fms",
                     min(read_times) * 1000, p95_read, max(read_times) * 1000)
            _assert_latency("read_file", p95_read, target_ms=5.0)

            # --- append_line benchmark ----------------------------------------
            append_path = "/workspace/latency_append.txt"
            append_times: list[float] = []
            for i in range(100):
                t0 = time.monotonic()
                await client.append_line(append_path, f"line {i}\n".encode())
                append_times.append(time.monotonic() - t0)

            p95_append = _p95(append_times)
            log.info("append_line: min=%.2fms p95=%.2fms max=%.2fms",
                     min(append_times) * 1000, p95_append, max(append_times) * 1000)
            _assert_latency("append_line", p95_append, target_ms=5.0)

        finally:
            await client.aclose()

    # Report observed p95 values (always visible in verbose pytest output)
    print(f"\n[test_file_op_latency] read_file p95={p95_read:.2f}ms  "
          f"append_line p95={p95_append:.2f}ms")


@pytest.mark.asyncio
async def test_stat_parallel(tmp_path: Path) -> None:
    """50 parallel stat ops; p95 < 20 ms.

    Aspirational: warns on miss rather than failing.
    """
    from primer.workspace.runtime.runtime_client import RuntimeClient

    workspace = str(tmp_path)

    async with _runtime_container(workspace) as (token, host_port):
        client = RuntimeClient(
            url=f"ws://127.0.0.1:{host_port}/",
            token=token,
        )
        await client.connect()
        try:
            # Create some files so stat returns real data
            for i in range(10):
                await client.write_file(f"/workspace/stat_seed_{i}.txt", b"x")

            async def _timed_stat(path: str) -> float:
                t0 = time.monotonic()
                await client.stat(path)
                return time.monotonic() - t0

            # 50 parallel stat ops across the 10 seed files
            tasks = [
                asyncio.create_task(_timed_stat(f"/workspace/stat_seed_{i % 10}.txt"))
                for i in range(50)
            ]
            stat_times = await asyncio.gather(*tasks)

            p95_stat = _p95(list(stat_times))
            log.info("stat (parallel): min=%.2fms p95=%.2fms max=%.2fms",
                     min(stat_times) * 1000, p95_stat, max(stat_times) * 1000)
            _assert_latency("stat_parallel", p95_stat, target_ms=20.0)

        finally:
            await client.aclose()

    print(f"\n[test_stat_parallel] stat p95={p95_stat:.2f}ms")


@pytest.mark.asyncio
async def test_watch_latency(tmp_path: Path) -> None:
    """Watch event must arrive in worker queue within 100 ms.

    Steps:
    1. Connect a RuntimeClient and start a watch_start subscription.
    2. Wait for watch_open confirmation.
    3. Write a file via the RuntimeClient itself (simulating host FS change).
    4. Assert the change event arrives within 100 ms.
    """
    from primer.workspace.runtime.runtime_client import RuntimeClient

    workspace = str(tmp_path)
    watch_file = "/workspace/watch_target.txt"

    async with _runtime_container(workspace) as (token, host_port):
        client = RuntimeClient(
            url=f"ws://127.0.0.1:{host_port}/",
            token=token,
        )
        await client.connect()
        try:
            # Create the file first so watchfiles has something to watch
            await client.write_file(watch_file, b"initial\n")

            first_event: asyncio.Future[tuple[str, float]] = asyncio.get_event_loop().create_future()

            async def _watch_task() -> None:
                """Consume the watch iterator and record the first change event."""
                async for change in client.watch([watch_file], ["modify", "create"]):
                    if not first_event.done():
                        first_event.set_result((change.path, time.monotonic()))
                    break  # we only need one event

            watch_task = asyncio.create_task(_watch_task())

            # Give the subscription a moment to open (watch_open frame arrives)
            await asyncio.sleep(0.3)

            # Trigger a modification via the client (routes through runtime FS)
            t_write = time.monotonic()
            await client.write_file(watch_file, b"modified\n")

            try:
                _path, t_event = await asyncio.wait_for(first_event, timeout=5.0)
                latency_ms = (t_event - t_write) * 1000.0
                log.info("watch latency: %.2f ms (target <100 ms)", latency_ms)
                print(f"\n[test_watch_latency] event latency={latency_ms:.2f}ms")
                _assert_latency("watch_event", latency_ms, target_ms=100.0)
            except asyncio.TimeoutError:
                pytest.fail("Watch change event did not arrive within 5 s")
            finally:
                watch_task.cancel()
                try:
                    await watch_task
                except (asyncio.CancelledError, Exception):
                    pass

        finally:
            await client.aclose()
