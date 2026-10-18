"""``DockerRuntimeAdapter.create_sandbox`` runs the image the template names (platform revamp 2026-10, plan 08 task 5; design
14.2 and 20 Q6).

Until now the adapter ignored the image it was given: it always ran ``primer/workspace-runtime:1.1`` and only recorded the typed
image in a label, so the form's Image field did nothing. The image a template names is the image that runs now, and it must
contain the primer workspace runtime (the adapter sets no ``Cmd``: the image's own entrypoint is the runtime server). A template
that names none runs the built-in runtime image. A container whose runtime never answers fails with an error that names the
image (503), and the rollback still removes the container and the volume.

These run WITHOUT a Docker daemon, on the small stateful fake of ``test_docker_create_sandbox_rolls_back.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

aiodocker = pytest.importorskip("aiodocker")

import primer.workspace.runtime.docker as docker_mod  # noqa: E402
from primer.model.except_ import ConfigError  # noqa: E402
from primer.model.workspace import DOCKER_RUNTIME_IMAGE, ContainerReachabilityHostPort, ResourceLimits  # noqa: E402
from tests.workspace.test_docker_create_sandbox_rolls_back import (  # noqa: E402
    NAME,
    VOLUME,
    _adapter,
    _FakeDocker,
    _nothing_left,
)

CUSTOM = "registry.local/team/runtime:7"


class _RecordingDocker(_FakeDocker):
    """The rollback test's fake daemon, remembering the ``config`` every container was created with."""

    def __init__(self) -> None:
        super().__init__()
        self.configs: list[dict] = []
        self.containers.create_or_replace = AsyncMock(side_effect=self._record)

    async def _record(self, *, name: str, config: dict):
        self.configs.append(config)
        return await self._create_or_replace(name=name, config=config)


async def _create(docker: _RecordingDocker, *, image: str, pull_policy: str = "if_missing"):
    return await _adapter(docker).create_sandbox(
        name=NAME, image=image, command=["run"], env={}, workdir="/w", volume_name=VOLUME, volume_target="/data",
        extra_mounts=[], user=None, resources=ResourceLimits(), network="none", pull_policy=pull_policy,  # type: ignore[arg-type]
        reachability=ContainerReachabilityHostPort(), token="tok",
    )


def _ready(monkeypatch, sandbox=object()):
    async def ready(*args, **kwargs):
        return sandbox

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", ready)
    return sandbox


async def test_the_container_runs_the_template_image(monkeypatch):
    docker = _RecordingDocker()
    sentinel = _ready(monkeypatch)

    assert await _create(docker, image=CUSTOM) is sentinel

    (config,) = docker.configs
    assert config["Image"] == CUSTOM
    assert config["Labels"]["primer.workspace.template.image"] == CUSTOM
    docker.images.inspect.assert_awaited_with(CUSTOM)          # the presence check is for the image that will run


async def test_an_empty_image_runs_the_runtime_image(monkeypatch):
    docker = _RecordingDocker()
    _ready(monkeypatch)

    await _create(docker, image="")

    (config,) = docker.configs
    assert config["Image"] == "primer/workspace-runtime:1.1" == DOCKER_RUNTIME_IMAGE
    assert config["Labels"]["primer.workspace.template.image"] == DOCKER_RUNTIME_IMAGE
    docker.images.inspect.assert_awaited_with(DOCKER_RUNTIME_IMAGE)


async def test_the_old_module_constant_is_still_the_runtime_image():
    """Kept for the imports that name it; it is the model's constant now."""
    assert docker_mod._RUNTIME_IMAGE == DOCKER_RUNTIME_IMAGE


async def test_the_pull_policy_applies_to_the_template_image(monkeypatch):
    docker = _RecordingDocker()
    docker.images.pull = AsyncMock()
    _ready(monkeypatch)

    await _create(docker, image=CUSTOM, pull_policy="always")
    docker.images.pull.assert_awaited_once_with(CUSTOM)

    docker.images.pull.reset_mock()
    docker.images.inspect = AsyncMock(side_effect=Exception("404 no such image"))
    await _create(docker, image=CUSTOM, pull_policy="if_missing")      # absent locally: pulled
    docker.images.pull.assert_awaited_once_with(CUSTOM)


async def test_a_template_image_that_cannot_be_pulled_names_it(monkeypatch):
    docker = _RecordingDocker()
    docker.images.inspect = AsyncMock(side_effect=Exception("404 no such image"))
    docker.images.pull = AsyncMock(side_effect=aiodocker.DockerError(404, {"message": "pull access denied"}))
    _ready(monkeypatch)

    with pytest.raises(ConfigError) as caught:
        await _create(docker, image=CUSTOM)

    assert repr(CUSTOM) in str(caught.value)
    assert _nothing_left(docker) and docker.configs == []


async def test_a_runtime_that_never_answers_names_the_image(monkeypatch):
    docker = _RecordingDocker()

    async def silent(*args, **kwargs):
        raise TimeoutError("Runtime did not become ready within 30.0s")

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", silent)

    with pytest.raises(ConfigError) as caught:
        await _create(docker, image=CUSTOM)

    assert repr(CUSTOM) in caught.value.message
    assert "must contain the primer workspace runtime" in caught.value.message
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert _nothing_left(docker), "the rollback still removed the container and the volume"
    assert docker.removed == [f"container:{NAME}", f"volume:{VOLUME}"]


def test_the_silent_runtime_message_is_the_copy():
    assert docker_mod._silent_runtime_message("img:1") == (
        "the workspace runtime did not answer within 30s in a container of image 'img:1'. "
        "A custom image must contain the primer workspace runtime (build it FROM primer/workspace-runtime:1.1)."
    )


def test_the_silent_runtime_message_follows_the_ready_timeout(monkeypatch):
    monkeypatch.setattr(docker_mod, "_READY_TIMEOUT_S", 45.0)

    assert "did not answer within 45s in a container" in docker_mod._silent_runtime_message("img:1")


async def test_a_runtime_error_that_is_not_a_timeout_is_not_rewritten(monkeypatch):
    """Only "the runtime never answered" is the image's fault. A refused connection or a protocol error keeps its own text."""
    docker = _RecordingDocker()

    async def broken(*args, **kwargs):
        raise RuntimeError("protocol error: bad hello")

    monkeypatch.setattr(docker_mod, "_make_ws_sandbox", broken)

    with pytest.raises(RuntimeError, match="bad hello"):
        await _create(docker, image=CUSTOM)
    assert _nothing_left(docker)
