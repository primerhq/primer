"""Publish through the actual curated tool against an isolated local workspace."""

import json
import os
from unittest.mock import AsyncMock

import pytest

from primer.model.principal import PrincipalRef
from primer.model.service import Service, ServiceVersion, ServiceViewerAuth
from primer.model.yield_ import ToolContext
from primer.service.publish import service_named
from primer.toolset.crud import shipped_approvers, shipped_gate
from primer.toolset.system import build_system_toolset
from tests.bootstrap.test_prime_agents_md import local_prime  # noqa: F401 - fixture
from tests.service.test_publish_version import _Artifacts
from tests.toolset.test_system_reset_prime_instructions import _provider_registry


def context(role="admin", workspace_id="prime"):
    actor = PrincipalRef(type="user", id="test-person", display="Test", role=role, source="local") if role else None
    return ToolContext(tool_call_id="call", session_id="session", workspace_id=workspace_id, initiated_by=actor)


@pytest.fixture
async def publication(local_prime):
    sp, registry = local_prime
    blobs = _Artifacts()
    toolset = build_system_toolset(
        storage_provider=sp, provider_registry=_provider_registry(sp), workspace_registry=registry,
        artifact_storage_registry=AsyncMock(get_default=AsyncMock(return_value=blobs)),
    )
    workspace = await registry.get_workspace("prime")
    await workspace.write_file("site/index.html", b"hello")
    await workspace.write_file("site/assets/icon.png", b"\x89PNG\x00")
    return sp, toolset, workspace, blobs


async def call(toolset, ctx, **kwargs):
    return await toolset.call(tool_name="publish_service", arguments={"name": "my-app", "path": "site", **kwargs}, ctx=ctx)


async def test_publish_create_activate_then_publish_again(publication):
    sp, toolset, workspace, blobs = publication
    tool = next(t for t in [t async for t in toolset.list_tools()] if t.id == "publish_service")
    assert tool.required_role == "admin" and tool.requires_workspace and not tool.interruptible
    assert shipped_gate("system", tool.id).type == "required"
    assert shipped_approvers("system", tool.id) is not None
    first = await call(toolset, context())
    assert not first.is_error, first.output
    fields = json.loads(first.output)
    service = await sp.get_storage(Service).get(fields["service_id"])
    assert service.viewer_auth == ServiceViewerAuth.CONSOLE
    assert service.active_version_id == fields["version_id"]
    assert fields["url"] == "/svc/my-app/" and fields["version"] == 1
    version = await sp.get_storage(ServiceVersion).get(fields["version_id"])
    assert {p: blobs.stored[k] for p, k in version.files.items()} == {"index.html": b"hello", "assets/icon.png": b"\x89PNG\x00"}
    await sp.get_storage(Service).update(service.model_copy(update={"viewer_auth": ServiceViewerAuth.NONE}))
    await workspace.write_file("site/index.html", b"new")
    second = await call(toolset, context())
    assert not second.is_error, second.output
    assert json.loads(second.output)["version"] == 2
    current = await sp.get_storage(Service).get(service.id)
    assert current.active_version_id == json.loads(second.output)["version_id"]
    assert current.viewer_auth == ServiceViewerAuth.NONE
    assert blobs.stored[version.files["index.html"]] == b"hello"


@pytest.mark.parametrize(("ctx", "extra", "reason"), [
    (None, {}, "bad-request"),
    (context(None), {}, "forbidden"),
    (context("user"), {}, "forbidden"),
    (context(workspace_id="missing"), {}, "not-found"),
    (context(), {"workspace_id": "other"}, "validation-error"),
    (context(), {"viewer_auth": "none"}, "validation-error"),
    (context(), {"name": "Bad Name"}, "validation-error"),
])
async def test_identity_and_schema_refusals_leave_no_service(publication, ctx, extra, reason):
    sp, toolset, _, blobs = publication
    result = await call(toolset, ctx, **extra)
    assert result.is_error and json.loads(result.output)["type"] == reason, result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored


async def test_invalid_manifest_and_missing_toolset_do_not_autocreate(publication):
    sp, toolset, workspace, blobs = publication
    for manifest, expected in ((b"bad: setting", "service.yaml"), (b"tools:\n- toolset_id: missing", "unknown toolset")):
        await workspace.write_file("site/service.yaml", manifest)
        result = await call(toolset, context())
        assert result.is_error and expected in result.output, result.output
        assert await service_named(sp, "my-app") is None and not blobs.stored


async def test_managed_service_refused_before_capture(publication, monkeypatch):
    sp, toolset, workspace, blobs = publication
    managed = await sp.get_storage(Service).create(Service(name="my-app", description="", harness_id="harness"))
    capture = AsyncMock(side_effect=AssertionError("capture called"))
    monkeypatch.setattr(workspace, "capture_service_bundle", capture)
    result = await call(toolset, context())
    assert result.is_error and "harness" in result.output
    capture.assert_not_awaited()
    assert not blobs.stored and (await sp.get_storage(Service).get(managed.id)).active_version_id is None


@pytest.mark.parametrize("case", ["traversal", "root", "reserved", "link-dir", "link-file", "fifo", "hardlink", "size", "count"])
async def test_unsafe_or_oversized_capture_precedes_writes(publication, case):
    sp, toolset, workspace, blobs = publication
    root = workspace.root
    path = "site"
    if case == "traversal":
        path = "../site"
    elif case == "root":
        path = "."
    elif case == "reserved":
        path = workspace.template.state_path
    elif case == "link-dir":
        (root / "alias").symlink_to(root / "site", target_is_directory=True)
        path = "alias"
    elif case == "link-file":
        (root / "site/link").symlink_to(root / "site/index.html")
    elif case == "fifo":
        os.mkfifo(root / "site/pipe")
    elif case == "hardlink":
        os.link(root / "site/index.html", root / "site/copy")
    elif case == "size":
        with (root / "site/large").open("wb") as handle:
            handle.truncate(10 * 1024 * 1024 + 1)
    else:
        for i in range(199):
            (root / "site" / str(i)).touch()
    result = await call(toolset, context(), path=path)
    assert result.is_error and json.loads(result.output)["type"] == "bad-request", result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored


@pytest.mark.parametrize("managed", [False, True])
async def test_first_publish_racing_another_creator(publication, monkeypatch, managed):
    from primer.model.except_ import ConflictError

    sp, toolset, _, blobs = publication
    store = sp.get_storage(Service)
    create = store.create
    winner = Service(name="my-app", description="existing", harness_id="owner" if managed else None)

    async def concurrent_create(service):
        await create(winner)
        raise ConflictError("service name is taken")

    monkeypatch.setattr(store, "create", concurrent_create)
    result = await call(toolset, context())
    saved = await store.get(winner.id)
    if managed:
        assert result.is_error and "harness" in result.output
        assert not blobs.stored and saved.active_version_id is None
    else:
        assert not result.is_error, result.output
        assert json.loads(result.output)["service_id"] == winner.id
        assert saved.description == "existing" and saved.active_version_id == json.loads(result.output)["version_id"]


async def test_renamed_service_is_not_activated_after_capture(publication, monkeypatch):
    sp, toolset, workspace, blobs = publication
    store = sp.get_storage(Service)
    original = await store.create(Service(name="my-app", description="existing"))
    capture = workspace.capture_service_bundle

    async def rename_during_capture(path):
        data = await capture(path)
        await store.update(original.model_copy(update={"name": "other-app"}))
        return data

    monkeypatch.setattr(workspace, "capture_service_bundle", rename_during_capture)
    result = await call(toolset, context())
    assert result.is_error and json.loads(result.output)["type"] == "conflict"
    assert not blobs.stored and (await store.get(original.id)).active_version_id is None


async def test_runtime_response_is_size_checked_before_tar_parsing(publication, monkeypatch):
    import gzip
    from primer.service import bundle

    sp, toolset, workspace, blobs = publication
    monkeypatch.setattr(bundle, "MAX_ARCHIVE_BYTES", 256)
    response = gzip.compress(bytes(257))
    monkeypatch.setattr(workspace, "capture_service_bundle", AsyncMock(return_value=response))
    result = await call(toolset, context())
    assert result.is_error and "expanded archive limit" in result.output
    assert not blobs.stored and await service_named(sp, "my-app") is None


async def test_failed_second_artifact_leaves_no_active_version_or_partial_files(publication, monkeypatch):
    from primer.model.except_ import ProviderError

    sp, toolset, _, blobs = publication
    put = blobs.put

    async def fail_after_first(**kwargs):
        if blobs.stored:
            raise ProviderError("artifact storage unavailable")
        return await put(**kwargs)

    monkeypatch.setattr(blobs, "put", fail_after_first)
    result = await call(toolset, context())
    assert result.is_error and "artifact storage unavailable" in result.output
    assert not blobs.stored and len(blobs.deleted) == 1
    assert (await service_named(sp, "my-app")).active_version_id is None


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancellation"])
async def test_runtime_capture_abandonment_preserves_health_and_precedes_all_writes(publication, monkeypatch, cancel):
    import asyncio
    from types import SimpleNamespace

    from primer.workspace import directory_bundle
    from primer.workspace.runtime.runtime_client import RuntimeClient

    sp, toolset, workspace, blobs = publication
    client = RuntimeClient(url="ws://unused", token="offline")
    client._negotiated_version = "1.7"
    sent = asyncio.Event()
    captures = []

    async def send(frame):
        req = json.loads(frame)
        if req["op"] == "health":
            client._dispatch_text(json.dumps({"req_id": req["req_id"], "ok": True, "result": {}}))
        else:
            assert req["op"] == "capture_service_bundle" and req["args"]["path"] == "site"
            captures.append(req["req_id"])
            sent.set()  # The peer accepts this request and keeps answering health, but never returns the archive.

    client._ws = SimpleNamespace(closed=False, send_str=send)
    client._connected.set()
    monkeypatch.setattr(directory_bundle, "CAPTURE_TIMEOUT_SECONDS", .12)
    monkeypatch.setattr(workspace, "capture_service_bundle", lambda path: client.capture_service_bundle(path, ()))
    task = asyncio.create_task(call(toolset, context()))
    try:
        await asyncio.wait_for(sent.wait(), 2)
        await client.ping()
        assert set(client._pending) == set(captures)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, 2)
            assert result.is_error and json.loads(result.output)["type"] == "provider-error"
            assert "timed out" in result.output
        client._dispatch_text(json.dumps({"req_id": captures[0], "ok": True, "result": {"content_b64": ""}}))
        assert not client._pending and not blobs.stored
        assert await service_named(sp, "my-app") is None
        await client.ping()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_disconnected_runtime_capture_deadline_covers_connection_wait(monkeypatch):
    import asyncio
    from primer.model.except_ import ProviderError
    from primer.workspace import directory_bundle
    from primer.workspace.runtime.runtime_client import RuntimeClient

    client = RuntimeClient(url="ws://unused", token="offline")
    client._negotiated_version = "1.7"
    monkeypatch.setattr(directory_bundle, "CAPTURE_TIMEOUT_SECONDS", .03)
    with pytest.raises(ProviderError, match="timed out"):
        await asyncio.wait_for(client.capture_service_bundle("site", ()), 1)
    assert not client._pending
