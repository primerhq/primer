"""T3 generated import/toggle paths enforce the real REST access contract."""

import base64
import io
import json
import zipfile

import pytest

from primer.access.person import Person
from primer.api.route_tools import build_generated_route_toolsets
from primer.model.access import Access
from primer.model.collection import Collection
from tests.api.test_route_tools import _call
from tests.api.test_session_access_routes import cast as cast


def archive(text):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("Guide.md", text)
    return base64.b64encode(output.getvalue()).decode()


async def owned(app, cast):
    await app.state.storage_provider.get_storage(Collection).create(Collection(
        id="w7-owned", description="Owner and peers collection",
        access=Access(owners=[cast.carol_id], grants={cast.dave_id: "full", cast.erin_id: "read"}),
    ))
    return build_generated_route_toolsets(app).providers["collections_api"]


@pytest.mark.parametrize("tool", ["enable_collection_search", "disable_collection_search"])
async def test_full_peer_cannot_toggle_search_on_either_generated_or_rest_path(app, client, cast, tool):
    provider = await owned(app, cast)
    args = {"collection_id": "w7-owned"}
    body = {"embedder": {"provider_id": "not-consulted", "model": "m"}, "vector_store_provider_id": "not-consulted"}
    if tool.startswith("enable"):
        args["body"] = body
    result = await _call(provider, tool, args, Person(cast.dave_id, "user"))
    assert result.is_error and json.loads(result.output)["status"] == 403
    if tool.startswith("enable"):
        response = await cast.dave.put("/v1/collections/w7-owned/search", json=body)
    else:
        response = await cast.dave.delete("/v1/collections/w7-owned/search")
    assert response.status_code == 403
    assert json.loads(result.output)["type"] == response.json()["type"]
    assert "owner" in response.text
    assert (await app.state.storage_provider.get_storage(Collection).get("w7-owned")).search is None


async def test_full_peer_can_import_body_but_reader_cannot_import_or_change_it(app, client, cast):
    provider = await owned(app, cast)
    args = {"collection_id": "w7-owned", "parent": "", "conflict": "fail", "body": {"file": archive("# Cedar\nreal body")}}
    result = await _call(provider, "import_collection_zip", args, Person(cast.dave_id, "user"))
    assert not result.is_error, result.output
    assert json.loads(result.output)["created"] == ["guide"]
    read = await cast.erin.get("/v1/collections/w7-owned/documents", params={"path": "guide"})
    assert read.status_code == 200 and read.json()["content"] == "# Cedar\nreal body"
    args["conflict"] = "overwrite"
    args["body"]["file"] = archive("forbidden replacement")
    denied = await _call(provider, "import_collection_zip", args, Person(cast.erin_id, "user"))
    assert denied.is_error and json.loads(denied.output)["status"] == 403
    rest = await cast.erin.post("/v1/collections/w7-owned/import", files={"file": ("archive.zip", b"invalid")})
    assert rest.status_code == 403 and rest.json()["type"] == json.loads(denied.output)["type"]
    after = await cast.carol.get("/v1/collections/w7-owned/documents", params={"path": "guide"})
    assert after.json()["content"] == "# Cedar\nreal body"


@pytest.mark.parametrize("tool", ["enable_collection_search", "disable_collection_search", "import_collection_zip"])
async def test_hidden_collection_and_missing_collection_have_the_same_generated_refusal(app, client, cast, tool):
    provider = await owned(app, cast)
    args = {"collection_id": "w7-owned"}
    if tool.startswith("enable"):
        args["body"] = {"embedder": {"provider_id": "emb", "model": "m"}, "vector_store_provider_id": "ssp"}
    elif tool.startswith("import"):
        args["body"] = {"file": archive("x")}
    hidden = await _call(provider, tool, args, Person(cast.mallory_id, "user"))
    missing = await _call(provider, tool, {**args, "collection_id": "missing"}, Person(cast.mallory_id, "user"))
    assert hidden.is_error and missing.is_error
    assert json.loads(hidden.output)["status"] == json.loads(missing.output)["status"] == 404
    assert json.loads(hidden.output)["type"] == json.loads(missing.output)["type"]


@pytest.mark.parametrize("door", ["direct", "meta"])
@pytest.mark.parametrize("name", ["enable_collection_search", "disable_collection_search", "import_collection_zip"])
async def test_generated_shipped_write_gates_park_before_dispatch_and_bind_exact_card(app, client, cast, door, name):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from primer.agent.approval import ApprovalResolver
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.api.routers.tool_approval import _build_pending_response
    from primer.model.chat import ToolCallPart
    from primer.model.principal import PrincipalRef
    from primer.model.tool_approval import ToolApprovalPolicy
    from primer.model.yield_ import YieldToWorker
    from primer.bootstrap.seed import default_policy_id, ensure_crud_approval_policies
    from tests.e2e.test_wiki_w7_t3_lifecycle import action_arguments
    from tests.toolset.test_approval_twins import _Session

    provider = await owned(app, cast)
    app.state.provider_registry.install_generated_api_toolsets({"collections_api": provider})
    await ensure_crud_approval_policies(app.state.storage_provider)
    resolver = ApprovalResolver(storage=app.state.storage_provider.get_storage(ToolApprovalPolicy))
    manager = ToolExecutionManager.for_workspace(
        toolset_providers={"system": app.state.system_toolset, "collections_api": provider}, session=_Session(),
        approval_resolver=resolver, provider_registry=app.state.provider_registry,
        tools=["system__call_tool", "collections_api__" + name],
        initiated_by=PrincipalRef(type="user", id=cast.carol_id, user_id=cast.carol_id, role="user", display="carol", source="local"),
    )
    samples = action_arguments("w7-owned", "emb", "ssp", "Cedar")
    args = samples[{"enable_collection_search": "enable", "disable_collection_search": "disable", "import_collection_zip": "import"}[name]]
    call = ToolCallPart(id="one", name="collections_api__" + name, arguments=args)
    if door == "meta":
        call = ToolCallPart(id="one", name="system__call_tool", arguments={"toolset_id": "collections_api", "tool_name": name, "arguments": args})
    with pytest.raises(YieldToWorker) as parked:
        await manager.execute(call)
    meta = parked.value.yielded.resume_metadata
    assert meta["policy_id"] == default_policy_id("collections_api", name)
    assert meta["approval_type"] == "required"
    pending = _build_pending_response({}, SimpleNamespace(parked_at=datetime.now(UTC)), {"resume_metadata": meta}).model_dump(mode="json")
    assert pending["arguments"] == args
    assert pending["tool_name"] == ("collections_api__" + name if door == "direct" else name)
    assert pending["gate_id"]
    assert (await app.state.storage_provider.get_storage(Collection).get("w7-owned")).search is None
    assert (await cast.carol.get("/v1/collections/w7-owned/documents")).json()["documents"] == []
