"""Integration tests for DockerRuntimeAdapter.

These tests require:
- Docker to be running (checked via aiodocker import + availability)
- The runtime image ``matrix/workspace-runtime:1.0`` to be pre-built
  (Task 6 is responsible for building it; these tests assume it exists)

Skip gracefully when either precondition is absent.
"""

from __future__ import annotations

import asyncio
import pytest

aiodocker = pytest.importorskip("aiodocker", reason="aiodocker not installed")


# ---------------------------------------------------------------------------
# Docker availability fixture
# ---------------------------------------------------------------------------


async def _docker_available() -> bool:
    """Return True if Docker daemon is reachable."""
    try:
        import aiodocker as _aiodocker
        docker = _aiodocker.Docker()
        await docker.version()
        await docker.close()
        return True
    except Exception:
        return False


async def _runtime_image_present() -> bool:
    """Return True if matrix/workspace-runtime:1.0 exists locally."""
    try:
        import aiodocker as _aiodocker
        docker = _aiodocker.Docker()
        await docker.images.inspect("matrix/workspace-runtime:1.0")
        await docker.close()
        return True
    except Exception:
        return False


# Run checks once at collection time (synchronously via asyncio.run).
try:
    _DOCKER_AVAILABLE = asyncio.run(_docker_available())
    _IMAGE_PRESENT = asyncio.run(_runtime_image_present())
except Exception:
    _DOCKER_AVAILABLE = False
    _IMAGE_PRESENT = False

pytestmark = pytest.mark.skipif(
    not _DOCKER_AVAILABLE or not _IMAGE_PRESENT,
    reason=(
        "Docker not available or matrix/workspace-runtime:1.0 not built. "
        "Run `docker build -t matrix/workspace-runtime:1.0 runtime/` first."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config():
    """Return a minimal DockerRuntimeConfig with default socket."""
    from primer.model.workspace import DockerRuntimeConfig
    return DockerRuntimeConfig()


def _make_resources():
    from primer.model.workspace import ResourceLimits
    return ResourceLimits()


async def _teardown(sandbox, volume_name: str, adapter) -> None:
    """Close RuntimeClient then stop/remove container and volume."""
    if sandbox is not None:
        # Close the underlying RuntimeClient to prevent unclosed-session warnings.
        try:
            await sandbox._client.aclose()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
        await sandbox.stop()
        await sandbox.remove()
    await adapter.remove_volume(volume_name)
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sandbox_returns_ws_sandbox() -> None:
    """create_sandbox should return a WSSandbox (not DockerSandbox)."""
    from primer.workspace.runtime.docker import DockerRuntimeAdapter
    from primer.workspace.runtime.ws_sandbox import WSSandbox

    adapter = DockerRuntimeAdapter(_make_config())
    await adapter.initialize()
    sandbox = None
    try:
        sandbox = await adapter.create_sandbox(
            name="test-ws-sandbox-create",
            image="matrix/workspace-runtime:1.0",
            command=[],
            env={},
            workdir="/workspace",
            volume_name="test-ws-vol-create",
            volume_target="/workspace",
            extra_mounts=[],
            user=None,
            resources=_make_resources(),
            network="full",
            pull_policy="never",
        )
        assert isinstance(sandbox, WSSandbox), (
            f"Expected WSSandbox, got {type(sandbox).__name__}"
        )
    finally:
        await _teardown(sandbox, "test-ws-vol-create", adapter)


@pytest.mark.asyncio
async def test_create_sandbox_injects_token() -> None:
    """Container should have MATRIX_RUNTIME_TOKEN in its environment."""
    import aiodocker as _aiodocker
    from primer.workspace.runtime.docker import DockerRuntimeAdapter

    adapter = DockerRuntimeAdapter(_make_config())
    await adapter.initialize()
    sandbox = None
    docker = _aiodocker.Docker()
    try:
        sandbox = await adapter.create_sandbox(
            name="test-ws-sandbox-token",
            image="matrix/workspace-runtime:1.0",
            command=[],
            env={},
            workdir="/workspace",
            volume_name="test-ws-vol-token",
            volume_target="/workspace",
            extra_mounts=[],
            user=None,
            resources=_make_resources(),
            network="full",
            pull_policy="never",
        )
        container = await docker.containers.get(sandbox.id[:12])
        info = await container.show()
        env_list = info.get("Config", {}).get("Env") or []
        assert any(e.startswith("MATRIX_RUNTIME_TOKEN=") for e in env_list), (
            f"MATRIX_RUNTIME_TOKEN not found in container env: {env_list}"
        )
    finally:
        await _teardown(sandbox, "test-ws-vol-token", adapter)
        await docker.close()


@pytest.mark.asyncio
async def test_create_sandbox_port_mapped() -> None:
    """Container should expose a host-side port mapping for 5959/tcp."""
    import aiodocker as _aiodocker
    from primer.workspace.runtime.docker import DockerRuntimeAdapter, _RUNTIME_PORT

    adapter = DockerRuntimeAdapter(_make_config())
    await adapter.initialize()
    sandbox = None
    docker = _aiodocker.Docker()
    try:
        sandbox = await adapter.create_sandbox(
            name="test-ws-sandbox-port",
            image="matrix/workspace-runtime:1.0",
            command=[],
            env={},
            workdir="/workspace",
            volume_name="test-ws-vol-port",
            volume_target="/workspace",
            extra_mounts=[],
            user=None,
            resources=_make_resources(),
            network="full",
            pull_policy="never",
        )
        container = await docker.containers.get(sandbox.id[:12])
        info = await container.show()
        ports = (
            info.get("NetworkSettings", {})
            .get("Ports", {})
            .get(f"{_RUNTIME_PORT}/tcp", [])
        ) or []
        assert ports, (
            f"No host port mapping found for {_RUNTIME_PORT}/tcp"
        )
        host_port = int(ports[0]["HostPort"])
        assert host_port > 0, f"Host port should be positive, got {host_port}"
    finally:
        await _teardown(sandbox, "test-ws-vol-port", adapter)
        await docker.close()


@pytest.mark.asyncio
async def test_read_write_via_ws_sandbox() -> None:
    """Basic read/write file operation via WSSandbox against live runtime."""
    from primer.workspace.runtime.docker import DockerRuntimeAdapter

    adapter = DockerRuntimeAdapter(_make_config())
    await adapter.initialize()
    sandbox = None
    try:
        sandbox = await adapter.create_sandbox(
            name="test-ws-sandbox-rw",
            image="matrix/workspace-runtime:1.0",
            command=[],
            env={},
            workdir="/workspace",
            volume_name="test-ws-vol-rw",
            volume_target="/workspace",
            extra_mounts=[],
            user=None,
            resources=_make_resources(),
            network="full",
            pull_policy="never",
        )
        content = b"hello from integration test\n"
        await sandbox.write_file("/workspace/test_rw.txt", content)
        read_back = await sandbox.read_file("/workspace/test_rw.txt")
        assert read_back == content, (
            f"Expected {content!r}, got {read_back!r}"
        )
    finally:
        await _teardown(sandbox, "test-ws-vol-rw", adapter)


@pytest.mark.asyncio
async def test_stop_and_remove_sandbox() -> None:
    """stop() and remove() should complete without raising."""
    from primer.workspace.runtime.docker import DockerRuntimeAdapter
    import aiodocker as _aiodocker

    adapter = DockerRuntimeAdapter(_make_config())
    await adapter.initialize()
    docker = _aiodocker.Docker()
    sandbox = None
    container_id = None
    try:
        sandbox = await adapter.create_sandbox(
            name="test-ws-sandbox-stop",
            image="matrix/workspace-runtime:1.0",
            command=[],
            env={},
            workdir="/workspace",
            volume_name="test-ws-vol-stop",
            volume_target="/workspace",
            extra_mounts=[],
            user=None,
            resources=_make_resources(),
            network="full",
            pull_policy="never",
        )
        container_id = sandbox.id
        # Close RuntimeClient before stopping container.
        await sandbox._client.aclose()  # noqa: SLF001
        await sandbox.stop()
        await sandbox.remove()
        sandbox = None
        # Confirm container no longer exists
        try:
            await docker.containers.get(container_id[:12])
            found = True
        except Exception:
            found = False
        assert not found, "Container should be removed after remove()"
    finally:
        if sandbox is not None:
            await _teardown(sandbox, "test-ws-vol-stop", adapter)
        else:
            await adapter.remove_volume("test-ws-vol-stop")
            await adapter.aclose()
        await docker.close()
