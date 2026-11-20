"""Publish -> versions -> activate/rollback -> retention (spec section 5)."""

from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timezone
import tarfile

import pytest


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _mk_service(client, name="pub-target"):
    r = await client.post(
        "/v1/services", json={"name": name, "description": "publish target"}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _publish(client, sid, files, **params):
    return await client.post(
        f"/v1/services/{sid}/versions",
        params=params,
        content=_tar(files),
        headers={"content-type": "application/gzip"},
    )


@pytest.mark.asyncio
async def test_publish_activates_and_lists(client):
    s = await _mk_service(client)
    r = await _publish(client, s["id"], {"index.html": b"<h1>v1</h1>"})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1
    svc = (await client.get(f"/v1/services/{s['id']}")).json()
    assert svc["active_version_id"] == v1["id"]
    listing = (await client.get(f"/v1/services/{s['id']}/versions")).json()
    assert len(listing["items"]) == 1
    assert listing["items"][0]["files"]["index.html"].startswith("artifact-")


@pytest.mark.asyncio
async def test_publish_no_activate_stages(client):
    s = await _mk_service(client, "stager")
    await _publish(client, s["id"], {"index.html": b"a"})
    r = await _publish(client, s["id"], {"index.html": b"b"}, activate="false")
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    svc = (await client.get(f"/v1/services/{s['id']}")).json()
    assert svc["active_version_id"] != r.json()["id"]


@pytest.mark.asyncio
async def test_publish_unknown_service_404(client):
    r = await _publish(client, "service-nope", {"index.html": b"x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_activate_rollback_and_cross_service_422(client):
    a = await _mk_service(client, "svc-a")
    b = await _mk_service(client, "svc-b")
    r1 = await _publish(client, a["id"], {"index.html": b"a1"})
    await _publish(client, a["id"], {"index.html": b"a2"})
    rb = await client.post(
        f"/v1/services/{a['id']}/_activate",
        json={"version_id": r1.json()["id"]},
    )
    assert rb.status_code == 200, rb.text
    assert rb.json()["active_version_id"] == r1.json()["id"]
    cross = await client.post(
        f"/v1/services/{b['id']}/_activate",
        json={"version_id": r1.json()["id"]},
    )
    assert cross.status_code == 422


@pytest.mark.asyncio
async def test_publish_validation_422_carries_field(client):
    s = await _mk_service(client, "badfn")
    r = await _publish(
        client, s["id"], {"index.html": b"x", "functions.py": b"def broken(:\n"}
    )
    assert r.status_code == 422, r.text
    assert "line" in r.text


@pytest.mark.asyncio
async def test_publish_unknown_toolset_422(client):
    s = await _mk_service(client, "badgrant")
    manifest = b"tools:\n  - toolset_id: ts-does-not-exist\n"
    r = await _publish(
        client, s["id"], {"index.html": b"x", "service.yaml": manifest}
    )
    assert r.status_code == 422, r.text
    assert "ts-does-not-exist" in r.text


@pytest.mark.asyncio
async def test_retention_keeps_20_never_active(client):
    s = await _mk_service(client, "retain")
    # v1 publishes and stays ACTIVE; the next 23 versions are staged
    # (activate=false), so pruning must drop old staged versions while
    # never touching the still-active v1.
    first = await _publish(client, s["id"], {"index.html": b"v1"})
    for i in range(2, 25):
        r = await _publish(
            client, s["id"], {"index.html": f"v{i}".encode()}, activate="false"
        )
        assert r.status_code == 201, r.text
    listing = (await client.get(f"/v1/services/{s['id']}/versions")).json()
    ids = [v["id"] for v in listing["items"]]
    assert len(ids) <= 21
    assert first.json()["id"] in ids
    svc = (await client.get(f"/v1/services/{s['id']}")).json()
    assert svc["active_version_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_activate_pruned_version_422(client):
    s = await _mk_service(client, "pruned")
    first = await _publish(client, s["id"], {"index.html": b"v1"})
    for i in range(2, 24):
        await _publish(client, s["id"], {"index.html": f"v{i}".encode()})
    # v1 was activated-then-superseded and eventually pruned; pointing
    # back at it must fail loudly, not serve a ghost.
    r = await client.post(
        f"/v1/services/{s['id']}/_activate",
        json={"version_id": first.json()["id"]},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_rename_after_real_publish_409(client):
    s = await _mk_service(client, "publocked")
    await _publish(client, s["id"], {"index.html": b"x"})
    current = (await client.get(f"/v1/services/{s['id']}")).json()
    current["name"] = "newname"
    r = await client.put(f"/v1/services/{s['id']}", json=current)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_delete_cascades_versions_after_publish(client):
    s = await _mk_service(client, "cascade")
    await _publish(client, s["id"], {"index.html": b"x"})
    await _publish(client, s["id"], {"index.html": b"y"})
    r = await client.delete(f"/v1/services/{s['id']}")
    assert r.status_code in (200, 204)
    listing = await client.get(f"/v1/services/{s['id']}/versions")
    assert listing.status_code == 404


async def _publish_tool(app, service_id, body, *, activate=True, role="admin"):
    from primer.access.acting import acting_as
    from primer.access.person import Person
    from primer.model.user import User

    users = app.state.storage_provider.get_storage(User)
    user_id = f"publish-test-{role}"
    if await users.get(user_id) is None:
        await users.create(User(
            id=user_id, username=user_id, password_hash="!test", role=role,
            created_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
        ))
    provider = app.state.generated_route_toolsets.providers["services"]
    with acting_as(Person(user_id, role)):
        return await provider.call(
            tool_name="publish_service_version",
            arguments={"service_id": service_id, "body": body, "activate": activate},
        )


@pytest.mark.asyncio
async def test_generated_publish_roundtrip_preserves_binary_and_activation(app, client):
    s = await _mk_service(client, "tool-publish")
    original = await _publish(client, s["id"], {"index.html": b"original"})
    assert original.status_code == 201, original.text
    binary = bytes(range(256))
    bundle = _tar({"index.html": b"from tool", "asset.bin": binary})
    encoded = base64.b64encode(bundle).decode("ascii")
    result = await _publish_tool(app, s["id"], encoded, activate=False)
    assert not result.is_error, result.output
    version = json.loads(result.output)
    assert version["version"] == 2
    assert set(version["files"]) == {"index.html", "asset.bin"}
    assert (await client.get(f"/v1/services/{s['id']}")).json()["active_version_id"] == original.json()["id"]
    result = await _publish_tool(app, s["id"], encoded)
    assert not result.is_error, result.output
    active = json.loads(result.output)
    assert active["version"] == 3
    assert (await client.get(f"/v1/services/{s['id']}")).json()["active_version_id"] == active["id"]
    page = await client.get("/svc/tool-publish/")
    assert page.status_code == 200 and page.content == b"from tool"
    asset = await client.get("/svc/tool-publish/asset.bin")
    assert asset.status_code == 200 and asset.content == binary


@pytest.mark.asyncio
async def test_generated_publish_schema_exposes_required_binary_body(app):
    operation = next(
        op for op in app.state.generated_route_toolsets.operations
        if op.tool.toolset_id == "services" and op.tool.id == "publish_service_version"
    )
    schema = operation.tool.args_schema
    assert "body" in schema["required"]
    assert schema["properties"]["body"]["type"] == "string"
    assert schema["properties"]["body"]["contentEncoding"] == "base64"
    assert operation.tool.required_role == "admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [base64.b64encode(b"not a tar").decode(), ""])
async def test_generated_publish_rejects_invalid_body_without_versions(app, client, body):
    s = await _mk_service(client, "tool-invalid")
    result = await _publish_tool(app, s["id"], body)
    assert result.is_error, result.output
    assert json.loads(result.output)["status"] == 422
    assert (await client.get(f"/v1/services/{s['id']}/versions")).json()["items"] == []
    assert (await client.get(f"/v1/services/{s['id']}")).json()["active_version_id"] is None


@pytest.mark.asyncio
async def test_generated_publish_requires_admin(app, client):
    s = await _mk_service(client, "tool-permissions")
    encoded = base64.b64encode(_tar({"index.html": b"denied"})).decode()
    result = await _publish_tool(app, s["id"], encoded, role="user")
    assert result.is_error, result.output
    assert json.loads(result.output)["status"] == 403
    assert (await client.get(f"/v1/services/{s['id']}/versions")).json()["items"] == []


@pytest.mark.asyncio
async def test_generated_publish_refuses_invalid_base64_before_dispatch(app, client):
    from primer.model.except_ import ConfigError

    s = await _mk_service(client, "tool-base64")
    # The shared binary transport raises ConfigError before any HTTP request.
    # Its caller (MCP or the agent tool executor) renders the tool error.
    with pytest.raises(ConfigError, match="body must be base64"):
        await _publish_tool(app, s["id"], "not-base64!")
    assert (await client.get(f"/v1/services/{s['id']}/versions")).json()["items"] == []
    assert (await client.get(f"/v1/services/{s['id']}")).json()["active_version_id"] is None


@pytest.mark.asyncio
async def test_generated_publish_preserves_missing_and_harness_guards(app, client):
    from primer.model.service import Service

    encoded = base64.b64encode(_tar({"index.html": b"guarded"})).decode()
    missing = await _publish_tool(app, "service-nope", encoded)
    assert missing.is_error and json.loads(missing.output)["status"] == 404
    s = await _mk_service(client, "tool-managed")
    store = app.state.storage_provider.get_storage(Service)
    row = await store.get(s["id"])
    row.harness_id = "harness-test"
    await store.update(row)
    managed = await _publish_tool(app, s["id"], encoded)
    assert managed.is_error and json.loads(managed.output)["status"] == 409
    assert json.loads(managed.output)["extensions"]["error"] == "managed_by_harness"
    assert (await client.get(f"/v1/services/{s['id']}/versions")).json()["items"] == []
