"""``DockerRuntimeAdapter.create_sandbox`` removes the container and the volume it made when it does not return a sandbox.

From ``containers.create_or_replace`` on there was no rollback at all: a readiness timeout (``_make_ws_sandbox`` polls for up to
30 s), a failed start or connect, or a cancel or a caller's ``asyncio.timeout`` anywhere in that stretch left the container
running and its volume behind, and the backend could not clean them up: it works on the sandbox ``create_sandbox`` returns, and
none was returned. The rollback removes the container BY NAME (the create may have been cancelled before it returned a handle) and
the volume only if this call made it, on its own task and bounded (``roll_back_shielded``).

A readiness timeout reaches the caller as the ``ConfigError`` that names the image (platform revamp 2026-10, plan 08 task 5:
the runtime never answered, so a custom image without it is the likeliest cause); it used to be the bare ``TimeoutError``
("Runtime did not become ready within 30.0s", a 500). What these tests pin is the rollback, which is unchanged: the container
and the volume this call made are removed whichever error ends it. A caller's own ``asyncio.timeout`` still surfaces as a
``TimeoutError`` (it reaches ``create_sandbox`` as a cancel, which is not the silent-runtime error).

These run WITHOUT a Docker daemon: a small stateful fake stands in for the aiodocker client.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

aiodocker = pytest.importorskip("aiodocker")

import primer.workspace.runtime.docker as docker_mod  # noqa: E402
from primer.model.except_ import ConfigError, ValidationError  # noqa: E402
from primer.model.workspace import (  # noqa: E402
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
    ResourceLimits,
)
from primer.workspace import base_backend  # noqa: E402
from primer.workspace.runtime.docker import DockerRuntimeAdapter  # noqa: E402
from primer.workspace.size_overrides import ProviderSizeRefused  # noqa: E402

NAME = "workspace-ws-1"
VOLUME = "workspace-ws-1-data"


def _not_found() -> Exception:
    return aiodocker.DockerError(404, {"message": "no such object"})


class _Handle:
    """What ``docker.containers.container(name)`` and ``docker.volumes.get(name)`` hand out: ``delete`` removes the object."""

    def __init__(self, owner: set[str], name: str, log: list[str], kind: str, gate: asyncio.Event | None, error: Exception | None):
        self._owner, self._name, self._log, self._kind, self._gate, self._error = owner, name, log, kind, gate, error
        self.delete_started = 0

    async def delete(self, **kwargs) -> None:
        self.delete_started += 1
        if self._gate is not None:
            await self._gate.wait()
        if self._error is not None:
            raise self._error
        if self._name not in self._owner:
            raise _not_found()
        self._owner.discard(self._name)
        self._log.append(f"{self._kind}:{self._name}")


class _FakeDocker:
    """The slice of the aiodocker client ``create_sandbox`` and its rollback use, with the daemon's state kept in two sets."""

    def __init__(self, *, volume_exists: bool = False) -> None:
        self.containers_present: set[str] = set()
        self.volumes_present: set[str] = {VOLUME} if volume_exists else set()
        self.removed: list[str] = []
        self.container_delete_gate: asyncio.Event | None = None
        self.container_delete_error: Exception | None = None
        self.container_handles: list[_Handle] = []
        self.volume_create_error: Exception | None = None
        self.create_or_replace_hook = None  # an async callable run INSTEAD of creating, to simulate an in-flight request
        self.start_error: Exception | None = None
        self.images = MagicMock()
        self.images.inspect = AsyncMock()
        self.volumes = MagicMock()
        self.volumes.get = AsyncMock(side_effect=self._volume_get)
        self.volumes.create = AsyncMock(side_effect=self._volume_create)
        self.containers = MagicMock()
        self.containers.create_or_replace = AsyncMock(side_effect=self._create_or_replace)
        self.containers.container = MagicMock(side_effect=self._container)

    async def _volume_get(self, name: str):
        if name not in self.volumes_present:
            raise _not_found()
        return _Handle(self.volumes_present, name, self.removed, "volume", None, None)

    async def _volume_create(self, config) -> None:
        if self.volume_create_error is not None:
            raise self.volume_create_error
        self.volumes_present.add(config["Name"])

    async def _create_or_replace(self, *, name: str, config):
        if self.create_or_replace_hook is not None:
            await self.create_or_replace_hook(self, name)
        self.containers_present.add(name)
        container = MagicMock()
        container.start = AsyncMock(side_effect=self.start_error)
        return container

    def _container(self, name: str) -> _Handle:
        handle = _Handle(self.containers_present, name, self.removed, "container", self.container_delete_gate, self.container_delete_error)
        self.container_handles.append(handle)
        return handle


def _adapter(docker: _FakeDocker) -> DockerRuntimeAdapter:
    cfg = ContainerWorkspaceConfig(
        runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(),
    )
    adapter = DockerRuntimeAdapter(cfg)
    adapter._docker = docker
    return adapter


async def _create(adapter: DockerRuntimeAdapter):
    return await adapter.create_sandbox(
        name=NAME, image="img:1", command=["run"], env={}, workdir="/w", volume_name=VOLUME, volume_target="/data",
        extra_mounts=[], user=None, resources=ResourceLimits(), network="none", pull_policy="if_missing",
        reachability=ContainerReachabilityHostPort(), token="tok",
    )


def _ws_sandbox(monkeypatch, behaviour):
    """Replace ``_make_ws_sandbox`` (the readiness wait and the connect) with ``behaviour``."""
    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", behaviour)


async def _readiness_times_out(*args, **kwargs):
    raise TimeoutError("Runtime did not become ready within 30.0s")


async def _until(condition, what: str) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


def _nothing_left(docker: _FakeDocker) -> bool:
    return docker.containers_present == set() and docker.volumes_present == set()


async def test_a_readiness_timeout_removes_the_container_and_the_volume_it_made(monkeypatch):
    docker = _FakeDocker()
    _ws_sandbox(monkeypatch, _readiness_times_out)
    with pytest.raises(ConfigError, match="did not answer"):
        await _create(_adapter(docker))
    assert _nothing_left(docker) and docker.removed == [f"container:{NAME}", f"volume:{VOLUME}"]


async def test_a_container_that_fails_to_start_is_removed_too(monkeypatch):
    docker = _FakeDocker()
    docker.start_error = RuntimeError("cannot start: port is already allocated")
    _ws_sandbox(monkeypatch, _readiness_times_out)  # never reached
    with pytest.raises(RuntimeError, match="cannot start"):
        await _create(_adapter(docker))
    assert _nothing_left(docker)


async def test_a_cancel_while_the_runtime_connects_is_rolled_back(monkeypatch):
    docker = _FakeDocker()
    entered = asyncio.Event()

    async def connecting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    _ws_sandbox(monkeypatch, connecting)
    task = asyncio.create_task(_create(_adapter(docker)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert _nothing_left(docker)


async def test_a_timeout_while_the_runtime_connects_is_rolled_back(monkeypatch):
    docker = _FakeDocker()

    async def connecting(*args, **kwargs):
        await asyncio.Event().wait()

    _ws_sandbox(monkeypatch, connecting)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await _create(_adapter(docker))
    assert _nothing_left(docker)


async def test_a_cancel_while_the_container_request_is_in_flight_removes_it_by_name(monkeypatch):
    """The request may have landed before the cancel did: there is no handle to remove it by, only its name."""
    docker = _FakeDocker()
    entered = asyncio.Event()

    async def request_in_flight(fake: _FakeDocker, name: str) -> None:
        fake.containers_present.add(name)  # the daemon carried the request out
        entered.set()
        await asyncio.Event().wait()

    docker.create_or_replace_hook = request_in_flight
    task = asyncio.create_task(_create(_adapter(docker)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert _nothing_left(docker)


async def test_a_volume_that_was_already_there_is_not_removed(monkeypatch):
    """A retry with the same id finds the data of an earlier life; it is not this call's to delete."""
    docker = _FakeDocker(volume_exists=True)
    _ws_sandbox(monkeypatch, _readiness_times_out)
    with pytest.raises(ConfigError):
        await _create(_adapter(docker))
    assert docker.containers_present == set() and docker.volumes_present == {VOLUME}
    assert docker.removed == [f"container:{NAME}"]
    docker.volumes.create.assert_not_awaited()


async def test_a_cancel_while_the_volume_request_is_in_flight_removes_the_volume(monkeypatch):
    docker = _FakeDocker()
    entered = asyncio.Event()

    async def volume_request_in_flight(config) -> None:
        docker.volumes_present.add(config["Name"])  # carried out by the daemon
        entered.set()
        await asyncio.Event().wait()

    docker.volumes.create.side_effect = volume_request_in_flight
    task = asyncio.create_task(_create(_adapter(docker)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert _nothing_left(docker)  # no container was ever made: its 404 is not an error


async def test_a_volume_the_daemon_refused_is_not_claimed(monkeypatch):
    """A 4xx on the create means nothing was made (here it raced another create); a later failure must not remove it."""
    docker = _FakeDocker()
    absent_at_the_check = {"first": True}
    real_get = docker._volume_get

    async def get(name):
        if absent_at_the_check.pop("first", False):
            raise _not_found()
        return await real_get(name)

    docker.volumes.get.side_effect = get
    docker.volume_create_error = aiodocker.DockerError(409, {"message": "volume name is already in use"})
    docker.volumes_present.add(VOLUME)  # the other create made it
    _ws_sandbox(monkeypatch, _readiness_times_out)
    with pytest.raises(ConfigError):
        await _create(_adapter(docker))
    assert docker.volumes_present == {VOLUME}


async def test_a_volume_request_that_dropped_the_connection_is_still_claimed(monkeypatch):
    """No answer is not a refusal: the daemon may have made the volume, so a failure later removes it."""
    docker = _FakeDocker()

    async def create_then_drop(config) -> None:
        docker.volumes_present.add(config["Name"])
        raise ConnectionResetError("the daemon went away after making it")

    docker.volumes.create.side_effect = create_then_drop
    _ws_sandbox(monkeypatch, _readiness_times_out)
    with pytest.raises(ConfigError):
        await _create(_adapter(docker))
    assert _nothing_left(docker)


async def test_a_second_cancel_during_the_rollback_does_not_abandon_it(monkeypatch):
    docker = _FakeDocker()
    docker.container_delete_gate = asyncio.Event()
    entered = asyncio.Event()

    async def connecting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    _ws_sandbox(monkeypatch, connecting)
    task = asyncio.create_task(_create(_adapter(docker)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        task.cancel()
        await _until(lambda: docker.container_handles and docker.container_handles[0].delete_started, "the rollback started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        docker.container_delete_gate.set()
    await _until(lambda: _nothing_left(docker), "the rollback finished after the second cancel")


async def test_a_rollback_that_hangs_does_not_hold_the_caller_past_its_bound(monkeypatch):
    monkeypatch.setattr(base_backend, "_ROLLBACK_WAIT_S", 0.05)
    docker = _FakeDocker()
    docker.container_delete_gate = asyncio.Event()
    _ws_sandbox(monkeypatch, _readiness_times_out)
    try:
        with pytest.raises(ConfigError, match="did not answer"):
            await asyncio.wait_for(_create(_adapter(docker)), timeout=2)
        assert docker.containers_present == {NAME}  # still being removed, in the background
    finally:
        docker.container_delete_gate.set()
    await _until(lambda: _nothing_left(docker), "the rollback finished in the background")


async def test_a_failing_container_removal_neither_masks_the_error_nor_skips_the_volume(monkeypatch):
    docker = _FakeDocker()
    docker.container_delete_error = aiodocker.DockerError(500, {"message": "the daemon fell over"})
    _ws_sandbox(monkeypatch, _readiness_times_out)
    with pytest.raises(ConfigError, match="did not answer"):
        await _create(_adapter(docker))
    assert docker.volumes_present == set() and docker.containers_present == {NAME}


async def test_a_create_that_succeeds_removes_nothing(monkeypatch):
    docker = _FakeDocker()
    sentinel = object()

    async def ready(*args, **kwargs):
        return sentinel

    _ws_sandbox(monkeypatch, ready)
    assert await _create(_adapter(docker)) is sentinel
    assert docker.removed == [] and docker.containers_present == {NAME} and docker.volumes_present == {VOLUME}


async def test_an_image_that_cannot_be_pulled_leaves_nothing_and_rolls_nothing_back():
    docker = _FakeDocker()
    docker.images.inspect = AsyncMock(side_effect=Exception("404 no such image"))
    docker.images.pull = AsyncMock(side_effect=aiodocker.DockerError(404, {"message": "no such image"}))
    with pytest.raises(ConfigError):
        await _create(_adapter(docker))
    assert _nothing_left(docker)
    docker.containers.container.assert_not_called()


async def test_a_cancel_before_the_container_request_leaves_a_live_container_of_that_name_alone():
    """A create with the id of a LIVE workspace, cancelled before it got to replace its container (here: while it makes the
    volume), removes the volume it made and must leave the container that holds the name alone."""
    docker = _FakeDocker()
    docker.containers_present.add(NAME)  # a live workspace holds the name
    entered = asyncio.Event()

    async def volume_request_in_flight(config) -> None:
        docker.volumes_present.add(config["Name"])
        entered.set()
        await asyncio.Event().wait()

    docker.volumes.create.side_effect = volume_request_in_flight
    task = asyncio.create_task(_create(_adapter(docker)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert docker.containers_present == {NAME}, "the live workspace's container was removed"
    assert docker.volumes_present == set()


async def test_a_container_request_the_daemon_refused_does_not_remove_what_holds_the_name():
    docker = _FakeDocker()
    docker.containers_present.add(NAME)

    async def refused(fake: _FakeDocker, name: str) -> None:
        raise aiodocker.DockerError(409, {"message": "conflict: the name is in use by a running container"})

    docker.create_or_replace_hook = refused
    with pytest.raises(aiodocker.DockerError):
        await _create(_adapter(docker))
    assert docker.containers_present == {NAME}  # not this call's to remove
    assert docker.volumes_present == set()  # the volume this call made is


async def test_a_container_request_that_dropped_the_connection_is_still_removed():
    """No answer is not a refusal: the daemon may have made the container before the connection went."""
    docker = _FakeDocker()

    async def made_then_dropped(fake: _FakeDocker, name: str) -> None:
        fake.containers_present.add(name)
        raise ConnectionResetError("the daemon went away after making it")

    docker.create_or_replace_hook = made_then_dropped
    with pytest.raises(ConnectionResetError):
        await _create(_adapter(docker))
    assert _nothing_left(docker)


# ---- the G5 audit, G5-07: a size the daemon refuses is a 422, and the rollback still runs ---------------------------------------------


async def _create_sized(adapter: DockerRuntimeAdapter, resources: ResourceLimits):
    return await adapter.create_sandbox(
        name=NAME, image="img:1", command=["run"], env={}, workdir="/w", volume_name=VOLUME, volume_target="/data",
        extra_mounts=[], user=None, resources=resources, network="full", pull_policy="if_missing",
        reachability=ContainerReachabilityHostPort(), token="tok",
    )


def _daemon_refuses(status: int, message: str, *, as_dict: bool = False):
    """What aiodocker raises for a refused request: 0.27 passes the message string, an older version the response body's dict."""
    async def refuse(fake: _FakeDocker, name: str) -> None:
        raise aiodocker.DockerError(status, {"message": message} if as_dict else message)
    return refuse


_TOO_MANY_CPUS = "Range of CPUs is from 0.01 to 16.00, as there are only 16 CPUs available"


@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize(("resources", "message", "fields"), [
    (ResourceLimits(cpu_cores=64), _TOO_MANY_CPUS, ["cpu_cores"]),
    (ResourceLimits(memory_bytes=2**20), "Minimum memory limit allowed is 6MB", ["memory_bytes"]),
    (ResourceLimits(cpu_cores=64, memory_bytes=2**20), _TOO_MANY_CPUS, ["cpu_cores"]),
    (ResourceLimits(cpu_cores=2), "NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler or the cgroup is not mounted", ["cpu_cores"]),
])
async def test_a_size_the_daemon_refuses_is_a_422_naming_the_field_and_the_rollback_still_runs(resources, message, fields, as_dict):
    """Inside the caps Docker refuses sizes of its own. The raw ``DockerError`` is no ``PrimerError``, so it used to answer the generic 500
    after the rollback; it is a ``ValidationError`` (422) naming the size field now, and what this call made is still removed."""
    docker = _FakeDocker()
    docker.create_or_replace_hook = _daemon_refuses(400, message, as_dict=as_dict)

    with pytest.raises(ProviderSizeRefused) as caught:
        await _create_sized(_adapter(docker), resources)

    assert isinstance(caught.value, ValidationError)
    assert caught.value.fields == fields and caught.value.reason == message
    assert caught.value.message == f"{', '.join(f'backend.{f}' for f in fields)}: Docker refused this size: {message}."
    assert isinstance(caught.value.__cause__, aiodocker.DockerError)
    assert docker.volumes_present == set() and docker.removed == [f"volume:{VOLUME}"], "the volume this call made is removed"
    assert docker.containers_present == set()


@pytest.mark.parametrize(("status", "message", "resources"), [
    (400, "invalid mount config for type \"bind\": bind source path does not exist", ResourceLimits(cpu_cores=64)),
    (400, _TOO_MANY_CPUS, ResourceLimits()),                 # the create set no CPU, so the refusal is not that size's
    (500, _TOO_MANY_CPUS, ResourceLimits(cpu_cores=64)),     # a server error is not a refusal of the request
    # The review of #869, F3: a refusal that only MENTIONS a CPU or memory word is not a size refusal.
    (404, "No such image: acme/memory-profiler:1", ResourceLimits(memory_bytes=2**30)),
    (400, 'invalid mount config for type "bind": bind source path does not exist: /sys/fs/cgroup/cpu', ResourceLimits(cpu_cores=2)),
    (409, 'Conflict. The container name "/workspace-cpu-lab" is already in use by container "abc"', ResourceLimits(cpu_cores=2)),
    (400, "invalid environment variable: MEMORY_LIMIT", ResourceLimits(memory_bytes=2**30)),
    (409, _TOO_MANY_CPUS, ResourceLimits(cpu_cores=64)),     # only the daemon's "invalid parameter" 400 carries a size refusal
])
async def test_a_failure_that_is_not_a_size_refusal_passes_on_as_it_was(status, message, resources):
    docker = _FakeDocker()
    docker.create_or_replace_hook = _daemon_refuses(status, message)

    with pytest.raises(aiodocker.DockerError) as caught:
        await _create_sized(_adapter(docker), resources)

    assert not isinstance(caught.value, ProviderSizeRefused) and caught.value.status == status
    assert docker.volumes_present == set()
