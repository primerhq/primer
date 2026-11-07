"""Safe initialization of a fresh Docker volume mounted at a custom workspace root.

These tests use a stateful Docker API fake. No Docker daemon is required.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import primer.workspace.runtime.docker as docker_mod
from primer.model.except_ import ConfigError
from primer.model.workspace import (
    DEFAULT_WORKSPACE_ROOT,
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
    ResourceLimits,
    VolumeMount,
)
from primer.workspace.runtime.docker import DockerRuntimeAdapter


NAME = "workspace-ws-custom"
VOLUME = f"{NAME}-data"
TARGET = "/srv/custom-work"
IMAGE = "registry.local/team/runtime:7"


class _DockerFailure(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"{status}: {message}")


class _Volume:
    def __init__(self, docker: "_Docker", name: str) -> None:
        self._docker = docker
        self._name = name

    async def show(self) -> dict[str, Any]:
        self._docker.events.append(f"volume.inspect:{self._name}")
        if self._name not in self._docker.volume_labels:
            raise _DockerFailure(404, "no such volume")
        return {"Labels": dict(self._docker.volume_labels[self._name])}

    async def delete(self, force: bool = False) -> None:
        self._docker.events.append(f"volume.delete:{self._name}")
        if self._name not in self._docker.volume_labels:
            raise _DockerFailure(404, "no such volume")
        self._docker.volume_labels.pop(self._name)
        self._docker.volume_modes.pop(self._name, None)
        self._docker.volume_entries.pop(self._name, None)


class _Volumes:
    def __init__(self, docker: "_Docker") -> None:
        self._docker = docker

    async def create(self, config: dict[str, Any]) -> _Volume:
        name = config["Name"]
        self._docker.events.append(f"volume.create:{name}")
        if self._docker.volume_create_gate is not None:
            if name not in self._docker.volume_labels:
                self._docker.volume_labels[name] = dict(config.get("Labels") or {})
                self._docker.volume_modes[name] = 0o755
                self._docker.volume_entries[name] = set()
            self._docker.volume_create_entered.set()
            await self._docker.volume_create_gate.wait()
        if name not in self._docker.volume_labels:
            self._docker.volume_labels[name] = dict(config.get("Labels") or {})
            self._docker.volume_modes[name] = 0o755
            self._docker.volume_entries[name] = set()
        return _Volume(self._docker, name)

    async def get(self, name: str) -> _Volume:
        if name not in self._docker.volume_labels:
            raise _DockerFailure(404, "no such volume")
        return _Volume(self._docker, name)


class _Container:
    def __init__(self, docker: "_Docker", name: str, config: dict[str, Any]) -> None:
        self._docker = docker
        self.name = name
        self.config = config

    @property
    def is_helper(self) -> bool:
        return self.config.get("Entrypoint") == ["/bin/chmod"]

    async def start(self) -> None:
        self._docker.events.append(f"container.start:{self.name}")

    async def wait(self) -> dict[str, int]:
        self._docker.events.append(f"container.wait:{self.name}")
        if self.is_helper and self._docker.helper_wait_gate is not None:
            self._docker.helper_wait_entered.set()
            await self._docker.helper_wait_gate.wait()
        code = self._docker.helper_exit_code if self.is_helper else 0
        if self.is_helper and code == 0:
            mount = self.config["HostConfig"]["Mounts"][0]
            self._docker.volume_modes[mount["Source"]] = 0o777
        return {"StatusCode": code}

    async def show(self) -> dict[str, Any]:
        self._docker.events.append(f"container.inspect:{self.name}")
        if self._docker.runtime_inspect_gate is not None:
            await self._docker.runtime_inspect_gate.wait()
        return {"State": dict(self._docker.runtime_state)}

    async def log(
        self,
        *,
        stdout: bool,
        stderr: bool,
        tail: int,
        timeout: float,
    ) -> list[str]:
        self._docker.events.append(f"container.log:{self.name}:{stdout}:{stderr}:{tail}")
        return self._docker.runtime_logs[-tail:]

    async def delete(self, *, force: bool, v: bool) -> None:
        await self._docker.delete_container(self.name, force=force, v=v)


class _Containers:
    def __init__(self, docker: "_Docker") -> None:
        self._docker = docker

    async def create_or_replace(self, *, name: str, config: dict[str, Any]) -> _Container:
        self._docker.events.append(f"container.create:{name}")
        self._docker.copy_up_empty_workdir_volume(config)
        container = _Container(self._docker, name, config)
        self._docker.containers_by_name[name] = container
        self._docker.created.append(container)
        return container

    def container(self, name: str) -> _Container:
        existing = self._docker.containers_by_name.get(name)
        if existing is not None:
            return existing
        return _MissingContainer(self._docker, name)


class _MissingContainer(_Container):
    def __init__(self, docker: "_Docker", name: str) -> None:
        super().__init__(docker, name, {})

    async def delete(self, *, force: bool, v: bool) -> None:
        raise _DockerFailure(404, "no such container")


class _Images:
    async def inspect(self, image: str) -> dict[str, Any]:
        return {}

    async def pull(self, image: str) -> None:
        return None


class _Docker:
    def __init__(
        self,
        *,
        volume_exists: bool = False,
        volume_labels: dict[str, str] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.created: list[_Container] = []
        self.containers_by_name: dict[str, _Container] = {}
        self.volume_labels: dict[str, dict[str, str]] = (
            {VOLUME: dict(volume_labels or {})} if volume_exists else {}
        )
        self.volume_modes: dict[str, int] = {VOLUME: 0o700} if volume_exists else {}
        self.volume_entries: dict[str, set[str]] = (
            {VOLUME: {"existing-user-file"}} if volume_exists else {}
        )
        self.helper_exit_code = 0
        self.helper_wait_gate: asyncio.Event | None = None
        self.helper_wait_entered = asyncio.Event()
        self.volume_create_gate: asyncio.Event | None = None
        self.volume_create_entered = asyncio.Event()
        self.runtime_state: dict[str, Any] = {
            "Status": "running",
            "ExitCode": 0,
            "Error": "",
        }
        self.runtime_logs: list[str] = []
        self.runtime_inspect_gate: asyncio.Event | None = None
        self.images = _Images()
        self.volumes = _Volumes(self)
        self.containers = _Containers(self)

    def copy_up_empty_workdir_volume(self, config: dict[str, Any]) -> None:
        """Model Docker creating WorkingDir before it populates an empty volume."""
        workdir = config.get("WorkingDir")
        mounts = config.get("HostConfig", {}).get("Mounts", [])
        for mount in mounts:
            if mount.get("Type") != "volume" or mount.get("Target") != workdir:
                continue
            source = mount["Source"]
            if self.volume_entries.get(source) != set():
                continue
            if mount.get("VolumeOptions", {}).get("NoCopy") is True:
                continue
            # The shipped image already owns /workspace at 0777. A missing custom
            # WorkingDir is created by Docker as root:root 0755 before copy-up.
            copied_mode = 0o777 if workdir == DEFAULT_WORKSPACE_ROOT else 0o755
            self.volume_modes[source] = copied_mode
            self.events.append(f"volume.copy-up:{source}:{copied_mode:o}")

    async def delete_container(self, name: str, *, force: bool, v: bool) -> None:
        self.events.append(f"container.delete:{name}:{force}:{v}")
        if name not in self.containers_by_name:
            raise _DockerFailure(404, "no such container")
        self.containers_by_name.pop(name)


@pytest.fixture(autouse=True)
def _aiodocker_import(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "aiodocker",
        SimpleNamespace(DockerError=_DockerFailure),
    )


def _adapter(docker: _Docker) -> DockerRuntimeAdapter:
    adapter = DockerRuntimeAdapter(ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
        reachability=ContainerReachabilityHostPort(),
    ))
    adapter._docker = docker
    return adapter


async def _create(
    docker: _Docker,
    monkeypatch,
    *,
    target: str = TARGET,
    ready: Any = None,
) -> Any:
    sentinel = object() if ready is None else ready

    async def connected(*args, **kwargs):
        if isinstance(sentinel, BaseException):
            raise sentinel
        return sentinel

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", connected)
    result = await _adapter(docker).create_sandbox(
        name=NAME,
        image=IMAGE,
        command=[],
        env={"VISIBLE": "env-private-42"},
        workdir=target,
        volume_name=VOLUME,
        volume_target=target,
        extra_mounts=[VolumeMount(source="/host/input", target="/input", read_only=True)],
        user="1234:5678",
        resources=ResourceLimits(cpu_cores=1, memory_bytes=1024 * 1024 * 32),
        network="full",
        pull_policy="if_missing",
        reachability=ContainerReachabilityHostPort(),
        token="runtime-secret",
    )
    return result


def _helper_and_final(docker: _Docker) -> tuple[_Container, _Container]:
    assert len(docker.created) == 2
    helper, final = docker.created
    assert helper.is_helper and not final.is_helper
    return helper, final


async def test_fresh_custom_volume_is_initialized_by_isolated_root_helper_before_final_runtime(monkeypatch) -> None:
    docker = _Docker()
    await _create(docker, monkeypatch)
    helper, final = _helper_and_final(docker)

    requested_labels = docker.volume_labels[VOLUME]
    assert set(requested_labels) == {docker_mod._VOLUME_CREATE_TOKEN_LABEL}
    assert requested_labels[docker_mod._VOLUME_CREATE_TOKEN_LABEL]
    assert docker.volume_modes[VOLUME] == 0o777
    assert set(helper.config) == {"Image", "Entrypoint", "Cmd", "User", "WorkingDir", "HostConfig", "Tty"}
    assert helper.config == {
        "Image": IMAGE,
        "Entrypoint": ["/bin/chmod"],
        "Cmd": ["0777", TARGET],
        "User": "0:0",
        "WorkingDir": "/",
        "HostConfig": {
            "Mounts": [{
                "Type": "volume", "Source": VOLUME, "Target": TARGET, "ReadOnly": False,
                "VolumeOptions": {"NoCopy": True},
            }],
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "Tty": False,
    }
    assert helper.name not in docker.containers_by_name
    assert final.config["User"] == "1234:5678"
    assert final.config["WorkingDir"] == TARGET
    assert "PRIMER_RUNTIME_TOKEN=runtime-secret" in final.config["Env"]
    assert final.config["HostConfig"]["Mounts"][0] == {
        "Type": "volume",
        "Source": VOLUME,
        "Target": TARGET,
        "ReadOnly": False,
        "VolumeOptions": {"NoCopy": True},
    }
    assert any(mount.get("Type") == "bind" for mount in final.config["HostConfig"]["Mounts"])
    helper_delete = f"container.delete:{helper.name}:True:False"
    assert docker.events.index(f"container.wait:{helper.name}") < docker.events.index(helper_delete)
    assert docker.events.index(helper_delete) < docker.events.index(f"container.create:{NAME}")


async def test_default_workspace_uses_image_mode_without_an_initializer(monkeypatch) -> None:
    docker = _Docker()
    await _create(docker, monkeypatch, target=DEFAULT_WORKSPACE_ROOT)

    assert len(docker.created) == 1
    final = docker.created[0]
    assert final.name == NAME and not final.is_helper
    assert final.config["HostConfig"]["Mounts"][0] == {
        "Type": "volume",
        "Source": VOLUME,
        "Target": DEFAULT_WORKSPACE_ROOT,
        "ReadOnly": False,
    }
    assert docker.volume_modes[VOLUME] == 0o777
    assert f"volume.copy-up:{VOLUME}:777" in docker.events
    assert not any(event.startswith("container.wait:") for event in docker.events)


async def test_custom_volume_target_is_validated_before_any_docker_write(monkeypatch) -> None:
    docker = _Docker()

    with pytest.raises(ValueError, match="other than '/'"):
        await _create(docker, monkeypatch, target="/")

    assert docker.events == []


@pytest.mark.parametrize(
    "labels",
    [{}, {docker_mod._VOLUME_CREATE_TOKEN_LABEL: "another-create"}],
    ids=["unlabelled", "mismatched"],
)
async def test_existing_volume_is_never_initialized_relabelled_or_deleted(monkeypatch, labels) -> None:
    docker = _Docker(volume_exists=True, volume_labels=labels)
    before_labels = dict(docker.volume_labels[VOLUME])
    before_mode = docker.volume_modes[VOLUME]

    with pytest.raises(ConfigError, match="did not answer"):
        await _create(docker, monkeypatch, ready=TimeoutError("silent runtime"))

    assert docker.volume_labels == {VOLUME: before_labels}
    assert docker.volume_modes == {VOLUME: before_mode}
    assert len(docker.created) == 1 and docker.created[0].name == NAME
    assert not any(event.startswith("container.wait:") for event in docker.events)
    assert f"volume.delete:{VOLUME}" not in docker.events


async def test_helper_failure_rolls_back_helper_then_label_proven_volume(monkeypatch) -> None:
    docker = _Docker()
    docker.helper_exit_code = 13

    with pytest.raises(ConfigError, match="chmod helper exited with status 13"):
        await _create(docker, monkeypatch)

    [helper] = docker.created
    assert helper.is_helper
    assert docker.events.index(f"container.delete:{helper.name}:True:False") < docker.events.index(f"volume.delete:{VOLUME}")
    assert docker.containers_by_name == {} and docker.volume_labels == {}


async def test_final_failure_rolls_back_final_then_label_proven_volume_after_helper_cleanup(monkeypatch) -> None:
    docker = _Docker()

    with pytest.raises(ConfigError, match="did not answer"):
        await _create(docker, monkeypatch, ready=TimeoutError("silent runtime"))

    helper, final = _helper_and_final(docker)
    events = docker.events
    assert events.index(f"container.delete:{helper.name}:True:False") < events.index(f"container.create:{NAME}")
    assert events.index(f"container.delete:{final.name}:True:False") < events.index(f"volume.delete:{VOLUME}")
    assert docker.containers_by_name == {} and docker.volume_labels == {}


async def test_readiness_timeout_logs_bounded_redacted_state_before_rollback(monkeypatch, caplog) -> None:
    docker = _Docker()
    docker.runtime_state = {
        "Status": "exited",
        "ExitCode": 127,
        "Error": f"failed under {TARGET} with runtime-secret and env-private-42\x00",
    }
    docker.runtime_logs = [
        "discard-prefix-" + "x" * (docker_mod._READINESS_LOG_MAX_CHARS + 50),
        f"last line: runtime-secret env-private-42 {TARGET}",
    ]

    with caplog.at_level(logging.WARNING, logger=docker_mod.__name__), pytest.raises(
        ConfigError, match="did not answer",
    ):
        await _create(docker, monkeypatch, ready=TimeoutError("silent runtime"))

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("docker runtime readiness timeout:")
    )
    assert "status='exited'" in message
    assert "exit_code=127" in message
    assert "owns_volume=True helper_ran=True" in message
    assert "runtime-secret" not in message
    assert "env-private-42" not in message
    assert TARGET not in message
    assert "discard-prefix" not in message
    assert "<redacted>" in message

    _, final = _helper_and_final(docker)
    log_event = f"container.log:{NAME}:True:True:{docker_mod._READINESS_LOG_TAIL_LINES}"
    assert docker.events.index(log_event) < docker.events.index(
        f"container.delete:{final.name}:True:False"
    )


async def test_readiness_diagnostics_are_time_bounded_before_rollback(monkeypatch, caplog) -> None:
    docker = _Docker()
    docker.runtime_inspect_gate = asyncio.Event()
    monkeypatch.setattr(docker_mod, "_READINESS_DIAGNOSTIC_TIMEOUT_S", 0.01)

    started = asyncio.get_running_loop().time()
    with caplog.at_level(logging.WARNING, logger=docker_mod.__name__), pytest.raises(
        ConfigError, match="did not answer",
    ):
        await asyncio.wait_for(
            _create(docker, monkeypatch, ready=TimeoutError("silent runtime")),
            timeout=1,
        )
    elapsed = asyncio.get_running_loop().time() - started

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("docker runtime readiness timeout:")
    )
    _, final = _helper_and_final(docker)
    assert elapsed < 1
    assert "diagnostic_timed_out=True" in message
    assert not any(event.startswith("container.log:") for event in docker.events)
    assert docker.events.index(f"container.inspect:{NAME}") < docker.events.index(
        f"container.delete:{final.name}:True:False"
    )


async def test_cancel_while_helper_waits_rolls_back_helper_and_owned_volume(monkeypatch) -> None:
    docker = _Docker()
    docker.helper_wait_gate = asyncio.Event()
    task = asyncio.create_task(_create(docker, monkeypatch))
    await asyncio.wait_for(docker.helper_wait_entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    [helper] = docker.created
    assert docker.events.index(f"container.delete:{helper.name}:True:False") < docker.events.index(f"volume.delete:{VOLUME}")
    assert docker.containers_by_name == {} and docker.volume_labels == {}


async def test_never_exiting_helper_times_out_and_rolls_back_before_final_create(monkeypatch) -> None:
    docker = _Docker()
    docker.helper_wait_gate = asyncio.Event()
    monkeypatch.setattr(docker_mod, "_WORKDIR_INIT_TIMEOUT_S", 0.01)

    started = asyncio.get_running_loop().time()
    with pytest.raises(ConfigError, match="chmod helper did not start and exit within 0.01s"):
        await asyncio.wait_for(_create(docker, monkeypatch), timeout=1)
    elapsed = asyncio.get_running_loop().time() - started

    [helper] = docker.created
    helper_delete = f"container.delete:{helper.name}:True:False"
    rollback_inspect = len(docker.events) - 2
    assert elapsed < 1
    assert docker.events[rollback_inspect:] == [
        f"volume.inspect:{VOLUME}",
        f"volume.delete:{VOLUME}",
    ]
    assert docker.events.index(helper_delete) < rollback_inspect
    assert f"container.create:{NAME}" not in docker.events
    assert docker.containers_by_name == {} and docker.volume_labels == {}


async def test_cancel_during_volume_create_deletes_only_the_token_proven_volume(monkeypatch) -> None:
    docker = _Docker()
    docker.volume_create_gate = asyncio.Event()
    task = asyncio.create_task(_create(docker, monkeypatch))
    await asyncio.wait_for(docker.volume_create_entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert docker.created == []
    assert docker.events[-2:] == [f"volume.inspect:{VOLUME}", f"volume.delete:{VOLUME}"]
    assert docker.volume_labels == {}
