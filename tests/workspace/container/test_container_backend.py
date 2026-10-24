"""Tests for ContainerWorkspaceBackend using a fake adapter.

The real runtime adapters (Docker / Podman / containerd) live in
:mod:`primer.workspace.runtime` and have their own gated contract
tests. The backend itself is unit-testable against a fake adapter that
returns :class:`FakeSandbox` instances.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

import pytest

from primer.int.sandbox import Sandbox
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityConfig,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    ResourceLimits,
    VolumeMount,
    WorkspaceResourceOverrides,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace.container.backend import ContainerWorkspaceBackend
from primer.workspace.runtime.adapter import ContainerRuntimeAdapter
from primer.workspace.sandbox.fake import FakeSandbox


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)


class _TrackingFakeSandbox(FakeSandbox):
    """FakeSandbox that notifies its adapter when removed."""

    def __init__(self, root: Path, *, sandbox_id: str, adapter: "_FakeAdapter") -> None:
        super().__init__(root=root, sandbox_id=sandbox_id)
        self._adapter_ref = adapter

    async def remove(self) -> None:
        self._adapter_ref._sandboxes.pop(self._id, None)
        await super().remove()


class _FakeAdapter(ContainerRuntimeAdapter):
    """Adapter that hands out FakeSandbox handles backed by tempdir slots."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        self._sandboxes: dict[str, _TrackingFakeSandbox] = {}
        self._volumes: set[str] = set()

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def create_sandbox(
        self, *,
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
        reachability: ContainerReachabilityConfig,
        token: str,
    ) -> Sandbox:
        root = self._tmp / name
        root.mkdir(parents=True, exist_ok=True)
        sb = _TrackingFakeSandbox(root=root, sandbox_id=name, adapter=self)
        # The backend reads ``mapped_host_port`` off the returned sandbox
        # for host_port reachability; the FakeSandbox represents both
        # modes here.
        sb.mapped_host_port = 32100
        self._sandboxes[name] = sb
        self._volumes.add(volume_name)
        return sb

    async def get_sandbox(self, name: str) -> Sandbox | None:
        return self._sandboxes.get(name)

    async def list_sandboxes(self) -> list[str]:
        return list(self._sandboxes)

    async def remove_volume(self, name: str) -> None:
        self._volumes.discard(name)


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest"),
    )


def _config() -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(
            socket_path="/var/run/docker.sock",
        ),
        reachability=ContainerReachabilityHostPort(),
    )


@pytest.mark.asyncio
async def test_create_then_get(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    ws = await backend.create(_template())
    fetched = await backend.get(ws.id)
    assert fetched is ws
    await backend.aclose()


@pytest.mark.asyncio
async def test_destroy_removes_sandbox_and_volume(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    ws = await backend.create(_template())
    wid = ws.id
    await backend.destroy(wid)
    assert wid not in await backend.list()
    # Volume removed by destroy.
    assert len(adapter._volumes) == 0
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_with_wrong_template_kind_raises(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    bad = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
    )  # default backend = local
    with pytest.raises(ConfigError, match="container"):
        await backend.create(bad)
    await backend.aclose()


@pytest.mark.asyncio
async def test_destroy_nonexistent_raises(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    with pytest.raises(NotFoundError):
        await backend.destroy("ws-nonexistent")
    await backend.aclose()


class _RecordingAdapter(_FakeAdapter):
    """Remembers what ``create_sandbox`` was asked for (the image to run, the size to apply)."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.calls: list[dict] = []

    async def create_sandbox(self, **kwargs) -> Sandbox:
        self.calls.append(kwargs)
        return await super().create_sandbox(**kwargs)


@pytest.mark.asyncio
async def test_an_empty_image_runs_the_runtime_image(tmp_path: Path) -> None:
    """Platform revamp 2026-10, design 14.2: the built-in runtime image is the default when a template names none. This used to
    pin the opposite (an empty image was a ``ConfigError``: "template has no image"), when the Docker adapter ran the runtime
    image whatever the template said; now the image a template names is the image that runs, and naming none runs the runtime."""
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image=""),
    )
    await backend.create(template)
    assert adapter.calls[0]["image"] == "primer/workspace-runtime:1.1"
    await backend.aclose()


@pytest.mark.asyncio
async def test_a_template_with_the_default_image_runs_the_runtime_image(tmp_path: Path) -> None:
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    await backend.create(WorkspaceTemplate(id="t1", provider_id="c1", description="", backend=ContainerTemplateConfig()))
    assert adapter.calls[0]["image"] == "primer/workspace-runtime:1.1"
    await backend.aclose()


@pytest.mark.asyncio
async def test_the_templates_own_image_reaches_the_adapter(tmp_path: Path) -> None:
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    await backend.create(_template())
    assert adapter.calls[0]["image"] == "alpine:latest"
    await backend.aclose()


@pytest.mark.asyncio
async def test_size_overrides_replace_the_template_cpu_and_memory(tmp_path: Path) -> None:
    """A per-workspace size (design 14.3) wins over the template's, field by field, for the CPU and memory Docker can apply."""
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest", cpu_cores=1, memory_bytes=2**30),
    )
    over = WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(cpu_cores=2, memory_bytes=2 * 2**30))
    await backend.create(template, overrides=over)
    resources = adapter.calls[0]["resources"]
    assert (resources.cpu_cores, resources.memory_bytes) == (2.0, 2147483648)
    await backend.aclose()


@pytest.mark.asyncio
async def test_an_override_of_one_field_keeps_the_templates_other_field(tmp_path: Path) -> None:
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest", cpu_cores=1, memory_bytes=2**30),
    )
    await backend.create(template, overrides=WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(memory_bytes=2**31)))
    resources = adapter.calls[0]["resources"]
    assert (resources.cpu_cores, resources.memory_bytes) == (1.0, 2**31)
    await backend.aclose()


@pytest.mark.asyncio
async def test_without_overrides_the_template_size_is_used(tmp_path: Path) -> None:
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest", cpu_cores=1, memory_bytes=2**30),
    )
    await backend.create(template)
    await backend.create(template, overrides=WorkspaceTemplateOverrides())
    await backend.create(template, overrides=WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides()))
    for call in adapter.calls:
        assert (call["resources"].cpu_cores, call["resources"].memory_bytes) == (1.0, 2**30)
    await backend.aclose()


@pytest.mark.asyncio
async def test_a_template_with_no_size_and_no_override_is_unlimited(tmp_path: Path) -> None:
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    await backend.create(_template())
    assert (adapter.calls[0]["resources"].cpu_cores, adapter.calls[0]["resources"].memory_bytes) == (None, None)
    await backend.aclose()


@pytest.mark.asyncio
async def test_a_disk_override_is_not_passed_on(tmp_path: Path) -> None:
    """Docker has no disk quota, and the registry refuses the override before it gets here; the backend does not invent one."""
    adapter = _RecordingAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    over = WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(disk_bytes=2**30))
    await backend.create(_template(), overrides=over)
    assert adapter.calls[0]["resources"].disk_bytes is None
    await backend.aclose()


@pytest.mark.asyncio
async def test_init_command_failure_rolls_back(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest"),
        init_commands=["false"],  # fails immediately
    )
    with pytest.raises(ConfigError, match="init command failed"):
        await backend.create(template)
    # Nothing should remain registered.
    assert await backend.list() == []
    await backend.aclose()


@pytest.mark.asyncio
async def test_get_evicts_gone_cached_handle_and_reattaches(
    tmp_path: Path,
) -> None:
    """Regression for the dead-handle cache bug: once a cached workspace's
    runtime client goes ``gone`` (the runtime self-evicts on a 404
    handshake), ``get()`` must evict it from the cache and re-attach a
    fresh handle rather than returning the dead one.
    """
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = _template()
    ws = await backend.create(template)
    wid = ws.id

    # Warm path: the cached handle is live and returned as-is.
    assert await backend.get(wid, template=template) is ws

    # Simulate the runtime self-evicting: the underlying sandbox reports
    # gone, so SandboxWorkspace.gone is True for the cached handle.
    ws.sandbox.gone = True
    assert ws.gone is True

    # The adapter still has a live (non-gone) sandbox under the same name
    # (e.g. a fresh pod/container that came back); a fresh re-attach picks
    # it up. Swap in a brand-new sandbox so we can prove a new handle.
    name = f"workspace-{wid}"
    fresh = _TrackingFakeSandbox(
        root=tmp_path / name, sandbox_id=name, adapter=adapter,
    )
    fresh.mapped_host_port = 32100
    adapter._sandboxes[name] = fresh

    # get() must NOT return the gone handle: it evicts + re-attaches.
    reattached = await backend.get(wid, template=template)
    assert reattached is not None
    assert reattached is not ws, "get() returned the dead, gone handle"
    assert reattached.gone is False
    # The fresh handle is now cached and stable on the next call.
    assert await backend.get(wid, template=template) is reattached
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_injects_strict_write_locking_env_when_enabled(
    tmp_path: Path,
) -> None:
    """When the template opts into strict write locking, the container env
    passed to ``create_sandbox`` carries ``PRIMER_STRICT_WRITE_LOCKING=1``."""
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()

    captured_env: dict[str, str] = {}
    orig_create_sandbox = adapter.create_sandbox

    async def _spy_create_sandbox(*, env, **kwargs):
        captured_env.update(env)
        return await orig_create_sandbox(env=env, **kwargs)

    adapter.create_sandbox = _spy_create_sandbox

    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest"),
        strict_write_locking=True,
    )
    await backend.create(template)
    assert captured_env.get("PRIMER_STRICT_WRITE_LOCKING") == "1"
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_omits_strict_write_locking_env_by_default(
    tmp_path: Path,
) -> None:
    """When the template does not opt in, the env var is absent entirely."""
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()

    captured_env: dict[str, str] = {}
    orig_create_sandbox = adapter.create_sandbox

    async def _spy_create_sandbox(*, env, **kwargs):
        captured_env.update(env)
        return await orig_create_sandbox(env=env, **kwargs)

    adapter.create_sandbox = _spy_create_sandbox

    await backend.create(_template())
    assert "PRIMER_STRICT_WRITE_LOCKING" not in captured_env
    await backend.aclose()


@pytest.mark.asyncio
async def test_get_returns_none_when_gone_handle_cannot_reattach(
    tmp_path: Path,
) -> None:
    """If the cached handle is gone AND the backing sandbox is also gone
    (adapter no longer has it), get() returns None after eviction."""
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = _template()
    ws = await backend.create(template)
    wid = ws.id

    ws.sandbox.gone = True
    # Remove the backing sandbox so re-attach finds nothing.
    adapter._sandboxes.pop(f"workspace-{wid}", None)

    assert await backend.get(wid, template=template) is None
    # The dead handle was evicted from the cache.
    assert wid not in backend._workspaces
    await backend.aclose()


@pytest.mark.asyncio
async def test_extra_mounts_reach_the_adapter_as_volume_mounts(tmp_path: Path) -> None:
    """INJ-05: the template's ContainerMount (host/container/readonly) must reach the adapter as the VolumeMount
    (source/target/read_only) it reads; passing the template objects through crashed provisioning on any mount."""
    seen: list = []

    class _RecordingAdapter(_FakeAdapter):
        async def create_sandbox(self, **kwargs) -> Sandbox:
            seen.extend(kwargs["extra_mounts"])
            return await super().create_sandbox(**kwargs)

    backend = ContainerWorkspaceBackend(_config(), adapter=_RecordingAdapter(tmp_path))
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(
            image="alpine:latest",
            extra_mounts=[{"host": "/srv/data", "container": "/data", "readonly": True}],
        ),
    )
    await backend.create(template)

    assert seen == [VolumeMount(source="/srv/data", target="/data", read_only=True)]
    await backend.aclose()


class _RefusingAdapter(_FakeAdapter):
    """An adapter whose daemon refuses the size it is given, as the Docker adapter reports it (the G5 audit, G5-07)."""

    def __init__(self, tmp_path: Path, fields: list[str]) -> None:
        super().__init__(tmp_path)
        self._fields = fields

    async def create_sandbox(self, **kwargs) -> Sandbox:
        from primer.workspace.size_overrides import ProviderSizeRefused

        raise ProviderSizeRefused("container", self._fields, "Range of CPUs is from 0.01 to 16.00")


def _sized_template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t-big", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest", cpu_cores=64, memory_bytes=2**30),
    )


@pytest.mark.asyncio
async def test_a_refused_size_from_the_override_is_named_where_the_request_sent_it(tmp_path: Path) -> None:
    from primer.workspace.size_overrides import ProviderSizeRefused

    backend = ContainerWorkspaceBackend(_config(), adapter=_RefusingAdapter(tmp_path, ["cpu_cores"]))
    await backend.initialize()
    over = WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(cpu_cores=64))

    with pytest.raises(ProviderSizeRefused) as caught:
        await backend.create(_sized_template(), overrides=over)

    assert caught.value.message == "overrides.resources.cpu_cores: Docker refused this size: Range of CPUs is from 0.01 to 16.00."
    assert caught.value.problem_extensions == {"errors": [{
        "loc": ["body", "overrides", "resources", "cpu_cores"],
        "msg": "Docker refused this size: Range of CPUs is from 0.01 to 16.00.",
        "type": "refused_by_provider",
    }]}
    await backend.aclose()


@pytest.mark.asyncio
async def test_a_refused_size_from_the_template_names_the_template_field(tmp_path: Path) -> None:
    from primer.workspace.size_overrides import ProviderSizeRefused

    backend = ContainerWorkspaceBackend(_config(), adapter=_RefusingAdapter(tmp_path, ["cpu_cores"]))
    await backend.initialize()
    only_memory = WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(memory_bytes=2**31))   # the CPU is the template's

    with pytest.raises(ProviderSizeRefused) as caught:
        await backend.create(_sized_template(), overrides=only_memory)

    assert caught.value.message == (
        "backend.cpu_cores: Docker refused this size: Range of CPUs is from 0.01 to 16.00. Template 't-big' sets backend.cpu_cores: "
        "edit the template, or give this workspace its own size in overrides.resources."
    )
    assert caught.value.problem_extensions == {}, "stored in the template, not sent in this request: no loc"
    await backend.aclose()
