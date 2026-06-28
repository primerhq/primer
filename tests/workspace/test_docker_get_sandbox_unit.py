"""Unit tests for DockerRuntimeAdapter.get_sandbox re-attach + helpers.

These run WITHOUT a real Docker daemon: the aiodocker client and the
``_make_ws_sandbox`` connect helper are mocked. They assert the
cross-process re-attach contract -- recovering the runtime bearer token
from the container env (``docker inspect`` -> ``Config.Env``) and
reconnecting -- which the gated tests in ``test_docker_backend.py``
exercise against a live daemon.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityBridge,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
)
import primer.workspace.runtime.docker as docker_mod
from primer.workspace.runtime.docker import DockerRuntimeAdapter, _token_from_inspect


# ---------------------------------------------------------------------------
# _token_from_inspect
# ---------------------------------------------------------------------------


def test_token_from_inspect_prefers_canonical_key() -> None:
    info = {"Config": {"Env": [
        "FOO=bar",
        "RUNTIME_TOKEN=alias-tok",
        "PRIMER_RUNTIME_TOKEN=canonical-tok",
    ]}}
    assert _token_from_inspect(info) == "canonical-tok"


def test_token_from_inspect_falls_back_to_alias() -> None:
    info = {"Config": {"Env": ["RUNTIME_TOKEN=alias-only"]}}
    assert _token_from_inspect(info) == "alias-only"


def test_token_from_inspect_absent() -> None:
    assert _token_from_inspect({"Config": {"Env": ["X=y"]}}) is None
    assert _token_from_inspect({"Config": {}}) is None
    assert _token_from_inspect({}) is None


def test_token_from_inspect_value_with_equals() -> None:
    """A token containing '=' (urlsafe b64 padding) round-trips."""
    info = {"Config": {"Env": ["PRIMER_RUNTIME_TOKEN=abc==def"]}}
    assert _token_from_inspect(info) == "abc==def"


# ---------------------------------------------------------------------------
# get_sandbox re-attach
# ---------------------------------------------------------------------------


def _cfg(reachability=None) -> ContainerWorkspaceConfig:
    kwargs: dict = dict(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
    )
    if reachability is not None:
        kwargs["reachability"] = reachability
    return ContainerWorkspaceConfig(**kwargs)


def _adapter_with_container(info: dict) -> tuple[DockerRuntimeAdapter, MagicMock]:
    container = MagicMock()
    container.show = AsyncMock(return_value=info)
    docker = MagicMock()
    docker.containers.get = AsyncMock(return_value=container)
    adapter = DockerRuntimeAdapter(_cfg(ContainerReachabilityHostPort()))
    adapter._docker = docker
    return adapter, container


@pytest.mark.asyncio
async def test_get_sandbox_reconnects_with_recovered_token(monkeypatch) -> None:
    """A running container yields a reconnected sandbox; the token comes
    from the container env and is stashed for the backend."""
    info = {
        "Id": "deadbeef",
        "State": {"Status": "running"},
        "Config": {"Env": ["PRIMER_RUNTIME_TOKEN=tok-xyz"]},
    }
    adapter, container = _adapter_with_container(info)

    captured: dict = {}
    fake_sandbox = MagicMock()

    async def _fake_make(docker, cont, name, token, *, reachability):
        captured["name"] = name
        captured["token"] = token
        captured["reachability"] = reachability
        return fake_sandbox

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)

    result = await adapter.get_sandbox("workspace-ws-1")
    assert result is fake_sandbox
    assert captured["name"] == "workspace-ws-1"
    assert captured["token"] == "tok-xyz"
    assert isinstance(captured["reachability"], ContainerReachabilityHostPort)
    # Token stashed so the backend can populate runtime_meta.
    assert fake_sandbox.recovered_token == "tok-xyz"


@pytest.mark.asyncio
async def test_get_sandbox_missing_container_returns_none() -> None:
    docker = MagicMock()
    docker.containers.get = AsyncMock(side_effect=Exception("404 no such container"))
    adapter = DockerRuntimeAdapter(_cfg(ContainerReachabilityHostPort()))
    adapter._docker = docker
    assert await adapter.get_sandbox("workspace-ws-x") is None


@pytest.mark.asyncio
async def test_get_sandbox_non_running_returns_none(monkeypatch) -> None:
    info = {
        "State": {"Status": "exited"},
        "Config": {"Env": ["PRIMER_RUNTIME_TOKEN=tok"]},
    }
    adapter, _ = _adapter_with_container(info)
    called = {"made": False}

    async def _fake_make(*a, **k):
        called["made"] = True
        return MagicMock()

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    assert await adapter.get_sandbox("workspace-ws-1") is None
    assert called["made"] is False


@pytest.mark.asyncio
async def test_get_sandbox_no_token_returns_none(monkeypatch) -> None:
    info = {"State": {"Status": "running"}, "Config": {"Env": ["X=y"]}}
    adapter, _ = _adapter_with_container(info)

    async def _fake_make(*a, **k):  # pragma: no cover -- must not be reached
        raise AssertionError("should not reconnect without a token")

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    assert await adapter.get_sandbox("workspace-ws-1") is None


@pytest.mark.asyncio
async def test_get_sandbox_reconnect_failure_returns_none(monkeypatch) -> None:
    info = {
        "State": {"Status": "running"},
        "Config": {"Env": ["PRIMER_RUNTIME_TOKEN=tok"]},
    }
    adapter, _ = _adapter_with_container(info)

    async def _boom(*a, **k):
        raise RuntimeError("ws handshake failed")

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _boom)
    assert await adapter.get_sandbox("workspace-ws-1") is None


@pytest.mark.asyncio
async def test_get_sandbox_uses_configured_reachability(monkeypatch) -> None:
    """The re-attach URL mode follows the provider's reachability config
    (bridge here), not the host_port default."""
    info = {
        "State": {"Status": "running"},
        "Config": {"Env": ["PRIMER_RUNTIME_TOKEN=tok"]},
    }
    container = MagicMock()
    container.show = AsyncMock(return_value=info)
    docker = MagicMock()
    docker.containers.get = AsyncMock(return_value=container)
    adapter = DockerRuntimeAdapter(
        _cfg(ContainerReachabilityBridge(network_name="primer-net")),
    )
    adapter._docker = docker

    captured: dict = {}

    async def _fake_make(docker, cont, name, token, *, reachability):
        captured["reachability"] = reachability
        return MagicMock()

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", _fake_make)
    await adapter.get_sandbox("workspace-ws-1")
    assert isinstance(captured["reachability"], ContainerReachabilityBridge)


# ---------------------------------------------------------------------------
# create_sandbox -- provisioning-failure mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sandbox_maps_image_pull_failure_to_config_error() -> None:
    """A missing/un-pullable runtime image must surface as ConfigError
    (-> 503 service-unavailable), not a raw aiodocker DockerError that would
    escape the workspace-create request as an unhandled 500."""
    from aiodocker import DockerError

    from primer.model.except_ import ConfigError
    from primer.model.workspace import ResourceLimits

    docker = MagicMock()
    docker.images.inspect = AsyncMock(side_effect=Exception("404 no such image"))
    docker.images.pull = AsyncMock(
        side_effect=DockerError(404, {"message": "no such image"}),
    )
    adapter = DockerRuntimeAdapter(_cfg(ContainerReachabilityHostPort()))
    adapter._docker = docker

    with pytest.raises(ConfigError):
        await adapter.create_sandbox(
            name="workspace-ws-1",
            image="img:1",
            command=["run"],
            env={},
            workdir="/w",
            volume_name="vol",
            volume_target="/data",
            extra_mounts=[],
            user=None,
            resources=ResourceLimits(),
            network="none",
            pull_policy="if_missing",
        )
    docker.images.pull.assert_awaited_once()
