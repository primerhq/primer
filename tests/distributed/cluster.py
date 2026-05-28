"""TestCluster — multi-process test harness for distributed-mode scenarios.

Spins up real OS subprocesses (primer API + worker) against a shared
Postgres database, waits for them to become healthy, and tears them down
cleanly on stop().

Usage::

    cluster = TestCluster(postgres_url="postgresql://...", api_count=2, worker_count=2)
    await cluster.start()
    async with cluster.client(0) as c:
        resp = await c.get("/v1/health")
    await cluster.stop()
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
import pytest


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProcessHandle:
    """A live (or terminated) subprocess managed by :class:`TestCluster`."""

    name: str           # "api-0", "worker-1", …
    pid: int
    port: int | None    # HTTP port (None for workers)
    popen: subprocess.Popen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_postgres_url(url: str) -> dict[str, Any]:
    """Parse a postgres:// URL into component parts.

    Handles both ``postgres://`` and ``postgresql://`` schemes and the
    ``driver+postgres://`` variant that testcontainers sometimes emits
    (e.g. ``postgresql+asyncpg://...``).
    """
    # Normalise scheme — strip driver prefix if present.
    normalised = url
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if normalised.startswith(prefix):
            normalised = "postgresql://" + normalised[len(prefix):]
    for prefix in ("postgresql://", "postgres://"):
        if normalised.startswith(prefix):
            normalised = "postgresql://" + normalised[len(prefix):]

    p = urlparse(normalised)
    return {
        "hostname": p.hostname or "localhost",
        "port": p.port or 5432,
        "username": p.username or "postgres",
        "password": p.password or "",
        "database": (p.path or "/postgres").lstrip("/") or "postgres",
    }


def _pg_env(postgres_url: str, schema: str) -> dict[str, str]:
    """Build the PRIMER_DB__* env-var dict for a Postgres connection.

    ``pydantic-settings`` uses ``__`` as the nested delimiter, so
    ``PRIMER_DB__PROVIDER=postgres`` maps to ``AppConfig.db.provider``.
    """
    parts = _parse_postgres_url(postgres_url)
    return {
        "PRIMER_DB__PROVIDER": "postgres",
        "PRIMER_DB__CONFIG__HOSTNAME": parts["hostname"],
        "PRIMER_DB__CONFIG__PORT": str(parts["port"]),
        "PRIMER_DB__CONFIG__USERNAME": parts["username"],
        "PRIMER_DB__CONFIG__PASSWORD": parts["password"],
        "PRIMER_DB__CONFIG__DATABASE": parts["database"],
        # Schema isolation — each cluster uses its own schema.
        "PRIMER_DB_SCHEMA": schema,
        # Use Postgres scheduler for cross-process event bus.
        "PRIMER_SCHEDULER__PROVIDER": "postgres",
        "PRIMER_SCHEDULER__CONFIG__HOSTNAME": parts["hostname"],
        "PRIMER_SCHEDULER__CONFIG__PORT": str(parts["port"]),
        "PRIMER_SCHEDULER__CONFIG__USERNAME": parts["username"],
        "PRIMER_SCHEDULER__CONFIG__PASSWORD": parts["password"],
        "PRIMER_SCHEDULER__CONFIG__DATABASE": parts["database"],
        "PRIMER_SCHEDULER__CONFIG__DB_SCHEMA": schema,
    }


# ---------------------------------------------------------------------------
# TestCluster
# ---------------------------------------------------------------------------


class TestCluster:
    """Manages a fleet of primer subprocesses against a shared Postgres.

    Parameters
    ----------
    postgres_url:
        A ``postgresql://user:pass@host:port/db`` connection string.
        Typically obtained from ``testcontainers.PostgresContainer``.
    api_count:
        Number of API-only processes to launch (``primer api --no-worker``).
    worker_count:
        Number of worker-only processes to launch (``primer worker``).
    start_port:
        First HTTP port assigned to API processes.  API-0 gets
        ``start_port``, API-1 gets ``start_port + 1``, etc.
    env_overrides:
        Additional env vars merged on top of the computed defaults.
        Use to inject feature flags or tune knobs per test.
    schema:
        Postgres schema to use.  Auto-generated from a UUID when None.
    """

    def __init__(
        self,
        *,
        postgres_url: str,
        api_count: int = 2,
        worker_count: int = 2,
        start_port: int = 8200,
        env_overrides: dict[str, str] | None = None,
        schema: str | None = None,
    ) -> None:
        self._postgres_url = postgres_url
        self._api_count = api_count
        self._worker_count = worker_count
        self._start_port = start_port
        self._env_overrides = env_overrides or {}
        self._schema = schema or f"test_{uuid.uuid4().hex[:8]}"

        self._api_handles: list[ProcessHandle] = []
        self._worker_handles: list[ProcessHandle] = []
        self._started = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def apis(self) -> list[ProcessHandle]:
        """All API process handles (in launch order)."""
        return list(self._api_handles)

    @property
    def workers(self) -> list[ProcessHandle]:
        """All worker process handles (in launch order)."""
        return list(self._worker_handles)

    @property
    def schema(self) -> str:
        """Postgres schema this cluster uses."""
        return self._schema

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Boot all processes; wait for each /v1/health to return 200.

        Raises ``TimeoutError`` if any API does not become healthy within
        30 seconds.
        """
        if self._started:
            raise RuntimeError("TestCluster.start() called twice")
        self._started = True

        base_env = {**os.environ, **_pg_env(self._postgres_url, self._schema)}
        base_env["PRIMER_LOG_JSON"] = "false"
        base_env["PRIMER_AUTO_BOOTSTRAP"] = "false"
        base_env.update(self._env_overrides)

        # Launch API processes.
        for i in range(self._api_count):
            port = self._start_port + i
            owner_prefix = f"api-{self._schema}-{i}"
            env = {
                **base_env,
                "PRIMER_PORT": str(port),
                "PRIMER_RUNTIME_MODE": "api",
                "PRIMER_OWNER_ID_PREFIX": owner_prefix,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "primer", "api", "--no-worker"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            handle = ProcessHandle(
                name=f"api-{i}",
                pid=proc.pid,
                port=port,
                popen=proc,
            )
            self._api_handles.append(handle)

        # Launch worker processes.
        for i in range(self._worker_count):
            owner_prefix = f"worker-{self._schema}-{i}"
            env = {
                **base_env,
                "PRIMER_RUNTIME_MODE": "worker",
                "PRIMER_OWNER_ID_PREFIX": owner_prefix,
                # Workers still need a port for /v1/health; give them
                # ports beyond the API range to avoid collisions.
                "PRIMER_PORT": str(
                    self._start_port + self._api_count + i
                ),
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "primer", "worker"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            handle = ProcessHandle(
                name=f"worker-{i}",
                pid=proc.pid,
                port=self._start_port + self._api_count + i,
                popen=proc,
            )
            self._worker_handles.append(handle)

        # Wait for all API processes to become healthy.
        await self._wait_for_all_healthy(timeout_s=30.0)

    async def stop(self) -> None:
        """SIGTERM all processes; join with 10s timeout; SIGKILL stragglers.

        Collects stdout/stderr from each process and attaches them to a
        pytest report via ``pytest.fail`` annotations if any process exited
        with a non-zero return code.
        """
        all_handles = self._api_handles + self._worker_handles

        # Send SIGTERM to everyone.
        for handle in all_handles:
            if handle.popen.returncode is None:
                try:
                    handle.popen.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass  # Already gone.

        # Give them 10 seconds to exit gracefully.
        deadline = asyncio.get_event_loop().time() + 10.0
        for handle in all_handles:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining > 0 and handle.popen.returncode is None:
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, handle.popen.wait
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    pass

        # SIGKILL stragglers.
        for handle in all_handles:
            if handle.popen.returncode is None:
                try:
                    handle.popen.kill()
                    handle.popen.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass

        # Collect logs and attach to pytest for debugging.
        self._collect_logs(all_handles)

    def _collect_logs(self, handles: list[ProcessHandle]) -> None:
        """Read stdout/stderr from each handle; warn via pytest if non-zero exit."""
        for handle in handles:
            proc = handle.popen
            # Communicate (non-blocking at this point — process is dead).
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            stdout_text = (stdout or b"").decode(errors="replace")
            stderr_text = (stderr or b"").decode(errors="replace")

            # rc=0 → clean exit; rc=-15 (SIGTERM) → expected kill; None → still running
            _clean_codes = {0, -int(signal.SIGTERM), -int(signal.SIGKILL)}
            if proc.returncode not in _clean_codes and proc.returncode is not None:
                # Non-clean exit — surface logs as a pytest warning so they
                # appear in the captured output even when the test passed.
                lines: list[str] = [
                    f"[TestCluster] {handle.name} exited with"
                    f" rc={proc.returncode}",
                ]
                if stdout_text.strip():
                    lines.append(f"--- stdout ---\n{stdout_text}")
                if stderr_text.strip():
                    lines.append(f"--- stderr ---\n{stderr_text}")
                pytest.fail("\n".join(lines), pytrace=False)

    # ------------------------------------------------------------------
    # Process control
    # ------------------------------------------------------------------

    async def kill(self, name: str, sig: int = signal.SIGTERM) -> None:
        """Send *sig* to the named process ("api-0", "worker-1", etc.).

        The cluster does NOT restart the process; the caller is
        responsible for any expected replacement behaviour.
        """
        handle = self._find(name)
        if handle.popen.returncode is not None:
            raise RuntimeError(
                f"Process {name!r} has already exited"
                f" (rc={handle.popen.returncode})"
            )
        handle.popen.send_signal(sig)

    def _find(self, name: str) -> ProcessHandle:
        all_handles = self._api_handles + self._worker_handles
        for h in all_handles:
            if h.name == name:
                return h
        raise KeyError(f"No process named {name!r} in cluster")

    # ------------------------------------------------------------------
    # Client helpers
    # ------------------------------------------------------------------

    def client(self, api_index: int) -> httpx.AsyncClient:
        """Return an :class:`httpx.AsyncClient` bound to API #{api_index}.

        The caller is responsible for entering and exiting the client
        as an async context manager::

            async with cluster.client(0) as c:
                resp = await c.get("/v1/health")
        """
        handle = self._api_handles[api_index]
        base_url = f"http://127.0.0.1:{handle.port}"
        return httpx.AsyncClient(base_url=base_url)

    @asynccontextmanager
    async def ws(self, api_index: int, path: str):
        """Open a WebSocket connection to API #{api_index} at *path*.

        Yields the connected :class:`websockets.ClientConnection`.

        Example::

            async with cluster.ws(0, "/v1/chats/abc/ws") as ws:
                msg = await ws.recv()
        """
        import websockets  # type: ignore[import-untyped]  # noqa: PLC0415

        handle = self._api_handles[api_index]
        url = f"ws://127.0.0.1:{handle.port}{path}"
        async with websockets.connect(url) as ws_conn:
            yield ws_conn

    # ------------------------------------------------------------------
    # Polling utility
    # ------------------------------------------------------------------

    async def wait_for(
        self,
        predicate: Callable[[], Any],
        *,
        timeout_s: float = 10.0,
        interval_s: float = 0.1,
    ) -> None:
        """Poll *predicate* until it returns a truthy value or *timeout_s* expires.

        *predicate* may be a plain callable or a coroutine function.
        Raises :exc:`TimeoutError` on expiry.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"wait_for predicate did not become True within"
                    f" {timeout_s}s"
                )
            await asyncio.sleep(min(interval_s, remaining))

    # ------------------------------------------------------------------
    # Internal: health polling
    # ------------------------------------------------------------------

    async def _wait_for_all_healthy(self, *, timeout_s: float) -> None:
        """Poll /v1/health on every API until all return 200 or timeout."""
        tasks = [
            asyncio.create_task(
                self._wait_one_healthy(handle, timeout_s=timeout_s),
                name=f"health-{handle.name}",
            )
            for handle in self._api_handles
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise errors[0]

    async def _wait_one_healthy(
        self, handle: ProcessHandle, *, timeout_s: float
    ) -> None:
        """Poll /v1/health for a single API handle until 200 or timeout."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        url = f"http://127.0.0.1:{handle.port}/v1/health"
        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=2.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                # Check if the subprocess died unexpectedly.
                if handle.popen.returncode is not None:
                    self._collect_logs([handle])
                    raise RuntimeError(
                        f"Process {handle.name!r} exited prematurely"
                        f" (rc={handle.popen.returncode}) before becoming healthy"
                    )
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return
                    last_error = RuntimeError(
                        f"GET {url} returned {resp.status_code}"
                    )
                except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_error = exc

                await asyncio.sleep(0.2)

        raise TimeoutError(
            f"API {handle.name!r} did not become healthy within"
            f" {timeout_s}s. Last error: {last_error}"
        )


# ---------------------------------------------------------------------------
# Self-tests (only run under -m distributed)
# ---------------------------------------------------------------------------


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_cluster_boots_2_apis_and_2_workers(cluster_2x2: TestCluster) -> None:
    """Basic smoke: cluster starts with the right process counts."""
    assert len(cluster_2x2.apis) == 2
    assert len(cluster_2x2.workers) == 2
    for api in cluster_2x2.apis:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{api.port}/v1/health"
            )
            assert resp.status_code == 200


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_cluster_stop_clean(postgres_container: str) -> None:
    """Cluster can start and stop; processes terminate after stop()."""
    cluster = TestCluster(
        postgres_url=postgres_container, api_count=1, worker_count=1
    )
    await cluster.start()
    await cluster.stop()
    assert cluster.apis[0].popen.returncode is not None
