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

# A digest that never matches a real capture: for the cases that refuse before
# the digest is compared (unsafe capture, bad manifest, harness check).
DIGEST = "a" * 64


async def digest_of(workspace, path):
    """The production digest: capture, validate, hash the validated file map."""
    import asyncio

    from primer.service.bundle import bundle_digest, validate_bundle

    data = await workspace.capture_service_bundle(path)
    bundle = await asyncio.to_thread(validate_bundle, data, workspace_capture=True)
    return bundle_digest(bundle)


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
    first = await call(toolset, context(), bundle_digest=await digest_of(workspace, "site"))
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
    # The content changed: the new capture needs a fresh inspected digest.
    second = await call(toolset, context(), bundle_digest=await digest_of(workspace, "site"))
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
    (context(workspace_id="missing"), {"bundle_digest": DIGEST}, "not-found"),
    (context(), {"workspace_id": "other"}, "validation-error"),
    (context(), {"viewer_auth": "none"}, "validation-error"),
    (context(), {"name": "Bad Name", "bundle_digest": DIGEST}, "validation-error"),
])
async def test_identity_and_schema_refusals_leave_no_service(publication, ctx, extra, reason):
    sp, toolset, _, blobs = publication
    result = await call(toolset, ctx, **extra)
    assert result.is_error and json.loads(result.output)["type"] == reason, result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored


async def test_invalid_manifest_and_missing_toolset_do_not_autocreate(publication):
    sp, toolset, workspace, blobs = publication
    await workspace.write_file("site/service.yaml", b"bad: setting")
    result = await call(toolset, context(), bundle_digest=DIGEST)
    assert result.is_error and "service.yaml" in result.output, result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored
    # This manifest validates; the refusal comes from the unknown toolset grant,
    # so the digest must match the (now different) content.
    await workspace.write_file("site/service.yaml", b"tools:\n- toolset_id: missing")
    result = await call(toolset, context(), bundle_digest=await digest_of(workspace, "site"))
    assert result.is_error and "unknown toolset" in result.output, result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored


async def test_managed_service_refused_before_capture(publication, monkeypatch):
    sp, toolset, workspace, blobs = publication
    managed = await sp.get_storage(Service).create(Service(name="my-app", description="", harness_id="harness"))
    capture = AsyncMock(side_effect=AssertionError("capture called"))
    monkeypatch.setattr(workspace, "capture_service_bundle", capture)
    result = await call(toolset, context(), bundle_digest=DIGEST)
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
    result = await call(toolset, context(), path=path, bundle_digest=DIGEST)
    assert result.is_error and json.loads(result.output)["type"] == "bad-request", result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored


@pytest.mark.parametrize("managed", [False, True])
async def test_first_publish_racing_another_creator(publication, monkeypatch, managed):
    from primer.model.except_ import ConflictError

    sp, toolset, workspace, blobs = publication
    store = sp.get_storage(Service)
    create = store.create
    winner = Service(name="my-app", description="existing", harness_id="owner" if managed else None)

    async def concurrent_create(service):
        await create(winner)
        raise ConflictError("service name is taken")

    monkeypatch.setattr(store, "create", concurrent_create)
    result = await call(toolset, context(), bundle_digest=await digest_of(workspace, "site"))
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
    # The digest is computed against the real capture, BEFORE the mock takes
    # over: the race the test plays out must happen inside the handler's own
    # capture, not in the test's setup.
    digest = await digest_of(workspace, "site")
    capture = workspace.capture_service_bundle

    async def rename_during_capture(path):
        data = await capture(path)
        await store.update(original.model_copy(update={"name": "other-app"}))
        return data

    monkeypatch.setattr(workspace, "capture_service_bundle", rename_during_capture)
    result = await call(toolset, context(), bundle_digest=digest)
    assert result.is_error and json.loads(result.output)["type"] == "conflict"
    assert not blobs.stored and (await store.get(original.id)).active_version_id is None


async def test_runtime_response_is_size_checked_before_tar_parsing(publication, monkeypatch):
    import gzip
    from primer.service import bundle

    sp, toolset, workspace, blobs = publication
    monkeypatch.setattr(bundle, "MAX_ARCHIVE_BYTES", 256)
    response = gzip.compress(bytes(257))
    monkeypatch.setattr(workspace, "capture_service_bundle", AsyncMock(return_value=response))
    result = await call(toolset, context(), bundle_digest=DIGEST)
    assert result.is_error and "expanded archive limit" in result.output
    assert not blobs.stored and await service_named(sp, "my-app") is None


async def test_failed_second_artifact_leaves_no_active_version_or_partial_files(publication, monkeypatch):
    from primer.model.except_ import ProviderError

    sp, toolset, workspace, blobs = publication
    put = blobs.put

    async def fail_after_first(**kwargs):
        if blobs.stored:
            raise ProviderError("artifact storage unavailable")
        return await put(**kwargs)

    monkeypatch.setattr(blobs, "put", fail_after_first)
    result = await call(toolset, context(), bundle_digest=await digest_of(workspace, "site"))
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
    task = asyncio.create_task(call(toolset, context(), bundle_digest=DIGEST))
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


# ---------------------------------------------------------------------------
# 5.8: the approval binds the content - inspect gives the digest, publish
# refuses a capture that no longer matches it.
# ---------------------------------------------------------------------------


async def test_inspect_reports_the_digest_and_file_list(local_prime):
    from primer.toolset._system_services import inspect_service_bundle_tool

    sp, registry = local_prime
    workspace = await registry.get_workspace("prime")
    await workspace.write_file("site/index.html", b"hello")
    await workspace.write_file("site/assets/icon.png", b"\x89PNG\x00")
    tool, inspect_handler = inspect_service_bundle_tool("system", sp, registry)
    assert tool.id == "inspect_service_bundle" and tool.read_only and tool.required_role == "admin"
    result = await inspect_handler({"path": "site"}, ctx=context())
    assert not result.is_error, result.output
    fields = json.loads(result.output)
    assert fields["files"] == ["assets/icon.png", "index.html"]
    assert fields["bundle_digest"] == await digest_of(workspace, "site")
    # Re-inspecting unchanged content is stable: the digest is canonical over
    # the validated file map, not over the capture's member order or framing.
    again = await inspect_handler({"path": "site"}, ctx=context())
    assert json.loads(again.output)["bundle_digest"] == fields["bundle_digest"]


async def test_inspect_refuses_non_admin_and_missing_workspace(local_prime):
    from primer.toolset._system_services import inspect_service_bundle_tool

    sp, registry = local_prime
    _, inspect_handler = inspect_service_bundle_tool("system", sp, registry)
    result = await inspect_handler({"path": "site"}, ctx=context("user"))
    assert result.is_error and json.loads(result.output)["type"] == "forbidden", result.output
    result = await inspect_handler({"path": "site"}, ctx=context(workspace_id="missing"))
    assert result.is_error and json.loads(result.output)["type"] == "not-found", result.output


async def test_content_changed_after_inspection_is_refused(publication):
    sp, toolset, workspace, blobs = publication
    digest = await digest_of(workspace, "site")
    await workspace.write_file("site/index.html", b"tampered")
    result = await call(toolset, context(), bundle_digest=digest)
    assert result.is_error and json.loads(result.output)["type"] == "conflict", result.output
    assert "changed" in result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored


async def test_a_digest_that_never_matched_is_refused(publication):
    sp, toolset, workspace, blobs = publication
    result = await call(toolset, context(), bundle_digest=DIGEST)
    assert result.is_error and json.loads(result.output)["type"] == "conflict", result.output
    assert "changed" in result.output
    assert await service_named(sp, "my-app") is None and not blobs.stored


async def test_unchanged_content_publishes_with_the_inspected_digest(publication):
    sp, toolset, workspace, blobs = publication
    result = await call(toolset, context(), bundle_digest=await digest_of(workspace, "site"))
    assert not result.is_error, result.output
    fields = json.loads(result.output)
    assert fields["version"] == 1 and fields["url"] == "/svc/my-app/"
    assert blobs.stored
