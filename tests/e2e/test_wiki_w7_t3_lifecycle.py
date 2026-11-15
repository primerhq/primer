"""CI-owned real collection ingestion/search gates with private SQLite and Lance.

The model and embedding HTTP responses are scripted. Auth, MCP, worker, approval,
archive import, content store, OpenAI adapter and Lance retrieval are production.
Import/collection start no server, and no shared install configuration is used.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import httpx
import pytest

from tests._support.mock_embeddings import EmbeddingScript, embedding_server
from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for
from tests.e2e.test_wiki_w7_t1_lifecycle import _share, isolated_config, mcp_http_client
from tests.e2e.test_wiki_w7_t2_lifecycle import stage_answer

REPO = Path(__file__).resolve().parents[2]
PAGES = ("runbooks/enable-collection-search", "runbooks/ingest-a-knowledge-base")
SPECS = {
    "reject_enable": ("enable_collection_search", "owner", "rejected"),
    "peer_enable": ("enable_collection_search", "peer", "approved"),
    "reader_import": ("import_collection_zip", "reader", "approved"),
    "enable": ("enable_collection_search", "owner", "approved"),
    "import": ("import_collection_zip", "owner", "approved"),
    "disable": ("disable_collection_search", "owner", "approved"),
}
ALLOWED = {
    "system__wiki_read", "system__describe_tool", "system__create_collection", "system__get_collection", "system__list_collections",
    "system__put_document", "system__get_document_content", "system__list_documents", "system__list_embedding_providers",
    "system__list_semantic_search_providers", "collections__search", "collections__collection_tree",
    *["collections_api__" + name for name in ("enable_collection_search", "get_collection_search_status", "disable_collection_search", "import_collection_zip")],
}


def zip_body(text: str) -> str:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr(zipfile.ZipInfo("Guides/Oncall.md", date_time=(2026, 1, 1, 0, 0, 0)), text)
        z.writestr(zipfile.ZipInfo("Policies/Handbook.txt", date_time=(2026, 1, 1, 0, 0, 0)), "Policy copy\n" + text)
    return base64.b64encode(output.getvalue()).decode()


def action_arguments(collection: str, embedder: str, ssp: str, text: str) -> dict:
    enable = {"collection_id": collection, "body": {
        "embedder": {"provider_id": embedder, "model": "w7-embedding"}, "vector_store_provider_id": ssp,
    }}
    upload = {"collection_id": collection, "parent": "", "conflict": "fail", "body": {"file": zip_body(text)}}
    return {"reject_enable": enable, "peer_enable": enable, "reader_import": upload,
            "enable": enable, "import": upload, "disable": {"collection_id": collection}}


def check_result(name: str, value) -> None:
    if name == "reject_enable":
        assert isinstance(value, dict) and value.get("rejected") is True
        assert value["tool_name"] == "enable_collection_search"
    elif name in {"peer_enable", "reader_import"}:
        assert isinstance(value, dict) and value.get("status") == 403
        assert ("owner" if name == "peer_enable" else "full") in value["detail"].casefold()
    elif name == "disable":
        assert value is None  # generated 204 has a JSON null tool result
    elif name == "enable":
        assert isinstance(value, dict) and value["search"]["state"] == "ready"
        assert value["search"]["error"] is None
    elif name == "import":
        assert isinstance(value, dict) and value["created"] == ["guides", "guides/oncall", "policies", "policies/handbook"]
        assert value["skipped"] == value["overwritten"] == value["rejected"] == []
    else:
        raise AssertionError("unknown collection stage")


class CollectionActionRule(Rule):
    def __init__(self, name: str, arguments: dict):
        super().__init__()
        self.name, self.arguments = name, arguments

    def matches(self, request: dict) -> bool:
        if not any(m.get("role") == "user" and m.get("content") == self.name for m in request.get("messages", [])):
            return False
        last = next((m for m in reversed(request["messages"]) if m.get("role") == "tool"), {})
        if last.get("tool_call_id") == self.name:
            value = json.loads(last["content"])
            check_result(self.name, value)
            self.emit_tool, self.emit_text = None, "RESULT:" + json.dumps(value, ensure_ascii=False)
        else:
            self.emit_text = None
            self.emit_tool, self.emit_tool_call_id = "system__call_tool", self.name
            self.emit_args = {"toolset_id": "collections_api", "tool_name": SPECS[self.name][0], "arguments": self.arguments}
        return True


def read_stage(items: list[dict], name: str) -> tuple[list[dict], str, object]:
    from primer.model.chat import Message, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord

    records, raw_errors = [], []
    expected_error = name in {"reject_enable", "peer_enable", "reader_import"}
    for row in items:
        if "kind" in row:
            SessionMessageRecord.model_validate(row)
            records.append(row)
        else:
            message = Message.model_validate(row)
            for part in message.parts:
                if isinstance(part, ToolResultPart) and part.error:
                    assert expected_error and part.id == name, row
                    check_result(name, json.loads(part.output))
                    raw_errors.append(part)
    assert len(raw_errors) == int(expected_error)
    event_errors = [r for r in records if r["kind"] == "tool_result" and r["payload"].get("error")]
    assert len(event_errors) == int(expected_error)
    if event_errors:
        check_result(name, json.loads(event_errors[0]["payload"]["output"]))
    answer, result = stage_answer(records)  # all child/root errors/cancels and final parent terminal checked
    check_result(name, result)
    raw_results = [p for row in items if "kind" not in row for p in Message.model_validate(row).parts if isinstance(p, ToolResultPart)]
    assert any(p.id == name and json.loads(p.output) == result and p.error is expected_error for p in raw_results)
    return records, answer, result


def prepare_wiki(root: Path) -> Path:
    wiki = root / "wiki"
    (wiki / "runbooks").mkdir(parents=True)
    (wiki / "index.md").write_text("---\ntitle: T3 acceptance\nsummary: Scoped collection runbooks.\n---\n[Runbooks](runbooks)\n")
    (wiki / "runbooks/index.md").write_text("---\ntitle: Collection runbooks\nsummary: The two T3 flows.\n---\n" + "\n".join(f"[{p}]({p})" for p in PAGES))
    for path in PAGES:
        shutil.copyfile(REPO / "docs/wiki" / (path + ".md"), wiki / (path + ".md"))
    return wiki


def start_server(root: Path, port: int, log):
    wiki = prepare_wiki(root)
    config = root / "config.json"
    config.write_text(json.dumps(isolated_config(root, port)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRIMER_")}
    env.update(PRIMER_WIKI_DIR=str(wiki))
    return subprocess.Popen([sys.executable, "-m", "primer", "api", "--config", str(config)], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)


@pytest.mark.asyncio
async def test_user_ingests_and_searches_collection_with_real_gates_and_access(mock_llm, unique_suffix, tmp_path, unused_tcp_port):
    from contextlib import AsyncExitStack
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    registry, llm_url = mock_llm
    model, provider, driver = f"w7-t3-{unique_suffix}", "w7-t3-llm", "w7-t3-driver"
    profile = profile_id_for(provider, model)
    collection, embedder, ssp = (f"w7-{kind}-{unique_suffix}" for kind in ("knowledge", "embedder", "vectors"))
    text = f"# Cedar on-call\nCedar escalation uses the blue pager {unique_suffix}.\n"
    actions = action_arguments(collection, embedder, ssp, text)
    registry.register(model, [CollectionActionRule(name, actions[name]) for name in SPECS])
    script = EmbeddingScript()
    evidence = REPO / "tests/.e2e/w7-t3"
    evidence.mkdir(parents=True, exist_ok=True)
    hashes = {p: hashlib.sha256((REPO / "docs/wiki" / (p + ".md")).read_bytes()).hexdigest() for p in PAGES}
    trace = {"case": "test_user_ingests_and_searches_collection_with_real_gates_and_access", "outcome": "running",
             "source_sha": os.environ.get("GITHUB_SHA", "local"), "runbook_hashes": hashes, "calls": [], "stages": {},
             "approvals": [], "identities": {}, "checks": {}, "text": text, "needle": unique_suffix, "collection": collection}
    process = None
    with embedding_server(script) as embedding_url:
        try:
            with (evidence / "server.stdout").open("wb") as log:
                process = start_server(tmp_path, unused_tcp_port, log)
                base = f"http://127.0.0.1:{unused_tcp_port}"
                async with AsyncExitStack() as stack:
                    admin = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=10))
                    deadline = asyncio.get_running_loop().time() + 45
                    while asyncio.get_running_loop().time() < deadline:
                        assert process.poll() is None, (evidence / "server.stdout").read_text()[-6000:]
                        try:
                            if (await admin.get("/v1/health")).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.2)
                    else:
                        pytest.fail("isolated T3 server did not become healthy")
                    for route in ("register", "login"):
                        response = await admin.post("/v1/auth/" + route, json={"username": "w7-t3-admin", "password": "w7-admin-password-123"})
                        assert response.status_code in (200, 201), response.text
                    response = await admin.get("/v1/auth/status")
                    assert response.status_code == 200 and response.json()["role"] == "admin"
                    trace["approver"] = response.json()["principal"]["user_id"]
                    setup = [
                        ("workspace_providers", {"id": "w7-local", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path / "workspaces")}}),
                        ("workspace_templates", {"id": "w7-template", "description": "T3 private template", "provider_id": "w7-local", "backend": {"kind": "local"}}),
                        ("llm_providers", {"id": provider, "provider": "openchat", "config": {"url": llm_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 2}}),
                        ("model_profiles", {"id": profile, "description": "T3 scripted model", "provider_id": provider, "model_name": model, "context_length": 32000}),
                        ("agents", {"id": driver, "description": "T3 private collection driver", "model": {"profile_id": profile}, "tools": ["system__call_tool"], "system_prompt": ["Perform one requested collection operation and wait for its approval."], "max_tool_turns": 8}),
                        ("embedding_providers", {"id": embedder, "provider": "openai", "models": [{"name": script.model}], "config": {"url": embedding_url, "flavor": "other"}, "limits": {"max_concurrency": 2}}),
                        ("ssp", {"id": ssp, "provider": "lance", "config": {"path": str(tmp_path / "vectors")}}),
                    ]
                    for plural, body in setup:
                        response = await admin.post("/v1/" + plural, json=body)
                        assert response.status_code == 201, response.text
                        await _share(admin, plural, body["id"])
                    assert (await admin.post("/v1/setup/seed")).status_code == 200
                    clients, workspaces = {}, {}
                    for role in ("owner", "peer", "reader"):
                        username, password = "w7-t3-" + role, "w7-temporary-password-123"
                        response = await admin.post("/v1/admin/users", json={"username": username, "password": password, "role": "user"})
                        assert response.status_code == 201, response.text
                        uid = response.json()["id"]
                        client = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=10))
                        assert (await client.post("/v1/auth/login", json={"username": username, "password": password})).status_code == 200
                        response = await client.get("/v1/auth/status")
                        assert response.status_code == 200 and response.json()["role"] == "user"
                        assert response.json()["principal"]["user_id"] == uid
                        trace["identities"][role] = uid
                        clients[role], workspaces[role] = client, response.json()["default_workspace_id"]
                    owner = clients["owner"]
                    response = await owner.get("/v1/workspaces/" + workspaces["owner"])
                    assert response.status_code == 200 and response.json()["is_default"]
                    default_before = response.json()
                    response = await admin.put("/v1/mcp_exposure", json={"enabled": True, "mode": "allowlist", "allowed_tools": sorted(ALLOWED)})
                    assert response.status_code in (200, 204), response.text
                    cookie = "; ".join(f"{k}={v}" for k, v in owner.cookies.items())
                    mcp_http = mcp_http_client(cookie)
                    await stack.enter_async_context(mcp_http)
                    read, write = await stack.enter_async_context(streamable_http_client(f"{base}/v1/mcp/", http_client=mcp_http))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    assert {t.name for t in (await session.list_tools()).tools} == {"list_tools", "find_tools", "call_tool"}

                    async def call(scoped, args, *, error=None):
                        result = await session.call_tool("call_tool", arguments={"scoped_id": scoped, "arguments": args})
                        envelope = json.loads("".join(p.text for p in result.content if hasattr(p, "text")))
                        trace["calls"].append({"scoped_id": scoped, "arguments": args, "envelope": envelope})
                        if error is not None:
                            assert envelope["ok"] is False and envelope["error"]["code"] == error, envelope
                            return envelope
                        assert not result.is_error and envelope["ok"] is True, envelope
                        return json.loads(envelope["output"])

                    for path in PAGES:
                        page = await call("system__wiki_read", {"path": path})
                        source = (REPO / "docs/wiki" / (path + ".md")).read_text().split("---", 2)[2].strip()
                        assert page["path"] == path and source in page["body"]
                    policies = {}
                    for name in ("enable_collection_search", "disable_collection_search", "import_collection_zip"):
                        detail = await call("system__describe_tool", {"toolset_id": "collections_api", "tool_name": name})
                        assert detail["required_role"] == "user" and detail["approval"]["gated"] and detail["approval"]["type"] == "required"
                        policies[name] = detail["approval"]["policy_id"]
                        assert policies[name]
                    # Native MCP deliberately keeps role/gate refusals opaque.
                    await call("system__list_embedding_providers", {"limit": 200}, error="tool_unavailable")
                    await call("system__list_semantic_search_providers", {"limit": 200}, error="tool_unavailable")
                    trace["checks"]["admin_provider_reads_refused"] = True
                    created = await call("system__create_collection", {"entity": {"id": collection, "description": "T3 private knowledge"}})
                    assert created["access"]["owners"] == [trace["identities"]["owner"]] and created["search"] is None
                    access = await owner.get(f"/v1/collections/{collection}/access")
                    assert access.status_code == 200
                    response = await owner.put(f"/v1/collections/{collection}/access", json={
                        "owners": [trace["identities"]["owner"]], "grants": {trace["identities"]["peer"]: "full", trace["identities"]["reader"]: "read"},
                        "public": False, "rev": access.json()["rev"],
                    })
                    assert response.status_code == 200, response.text
                    for path, content in (("overview", "Cedar initial operating instructions " + unique_suffix),
                                          ("quartz", "Quartz unrelated material " + unique_suffix),
                                          ("cedar-notes", "Cedar escalation notes " + unique_suffix)):
                        await call("system__put_document", {"collection_id": collection, "path": path, "content": content})
                        reread = await call("system__get_document_content", {"collection_id": collection, "path": path})
                        assert reread["content"] == content
                    disabled = await call("collections_api__get_collection_search_status", {"collection_id": collection})
                    assert disabled["state"] == "disabled"
                    unavailable = await call("collections__search", {"collection": collection, "query": "Cedar", "mode": "semantic"}, error="tool_failed")
                    assert "semantic search" in unavailable["error"]["message"]
                    await call("collections_api__enable_collection_search", actions["enable"], error="tool_unavailable")
                    assert (await call("collections_api__get_collection_search_status", {"collection_id": collection}))["state"] == "disabled"
                    trace["checks"]["unapproved_native_write_has_no_effect"] = True
                    flow_deadline = asyncio.get_running_loop().time() + 90

                    async def snapshot():
                        row = await owner.get(f"/v1/collections/{collection}")
                        assert row.status_code == 200
                        docs = await owner.get(f"/v1/collections/{collection}/documents")
                        assert docs.status_code == 200
                        return {"search": row.json()["search"], "access": row.json()["access"],
                                "documents": sorted(docs.json()["documents"], key=lambda d: d["path"])}

                    async def stage(name):
                        operation, role, decision = SPECS[name]
                        actor = clients[role]
                        prior = await snapshot()
                        response = await actor.post(f"/v1/workspaces/{workspaces[role]}/sessions", json={
                            "binding": {"kind": "agent", "agent_id": driver}, "initial_instructions": name,
                            "auto_start": True, "autonomous": True,
                        })
                        assert response.status_code == 201, response.text
                        started = response.json()
                        uid, sid = trace["identities"][role], started["id"]
                        assert started["initiated_by"]["user_id"] == uid and started["initiated_by"]["role"] == "user"
                        while asyncio.get_running_loop().time() < flow_deadline:
                            response = await admin.get(f"/v1/sessions/{sid}/tool_approval/pending")
                            if response.status_code == 200:
                                pending = response.json()
                                break
                            assert response.status_code == 404, response.text
                            await asyncio.sleep(0.1)
                        else:
                            pytest.fail(f"T3 stage {name} never parked")
                        assert pending["tool_name"] == operation and pending["approval_type"] == "required"
                        assert pending["policy_id"] == policies[operation] and pending["gate_id"]
                        assert pending["arguments"] == actions[name]
                        assert await snapshot() == prior  # no effect while awaiting the human
                        response = await admin.post(f"/v1/sessions/{sid}/tool_approval/respond", json={
                            "tool_call_id": pending["tool_call_id"], "gate_id": pending["gate_id"],
                            "decision": decision, "reason": "Deliberate private T3 " + name,
                        })
                        assert response.status_code == 202 and response.json() == {"status": "accepted"}, response.text
                        trace["approvals"].append({"stage": name, "operation": operation, "session_id": sid,
                                                   "policy_id": pending["policy_id"], "gate_id": pending["gate_id"],
                                                   "decision": decision, "decision_http": 202, "effect_absent_before": True,
                                                   "pending": pending})
                        while asyncio.get_running_loop().time() < flow_deadline:
                            response = await actor.get(f"/v1/sessions/{sid}")
                            assert response.status_code == 200, response.text
                            row = response.json()
                            assert row["initiated_by"]["user_id"] == uid
                            assert row["ended_reason"] not in {"failed", "cancelled"}, row
                            if row["turn_status"] == "idle" and row["ended_reason"] == "completed":
                                break
                            await asyncio.sleep(0.1)
                        else:
                            pytest.fail(f"T3 stage {name} never completed")
                        response = await admin.get(f"/v1/sessions/{sid}/messages", params={"limit": 200})
                        assert response.status_code == 200, response.text
                        items = response.json()["items"]
                        records, answer, result = read_stage(items, name)
                        actual = [json.loads(m["content"]) for req in registry.requests if req.get("model") == model
                                  for m in req.get("messages", []) if m.get("role") == "tool" and m.get("tool_call_id") == name]
                        assert actual and actual[-1] == result
                        after = await snapshot()
                        if name in {"reject_enable", "peer_enable", "reader_import"}:
                            assert after == prior, (prior, after)
                        trace["stages"][name] = {"session_id": sid, "actor": role, "initiator": uid, "started": started,
                                                 "completed": True, "answer": answer, "result": result, "items": items,
                                                 "records": records, "before": prior, "after": after}
                        return result

                    for name in ("reject_enable", "peer_enable", "reader_import"):
                        await stage(name)
                    trace["checks"]["rejection_and_nonowner_have_no_effect"] = True
                    enabled = await stage("enable")
                    assert enabled["access"]["owners"] == [trace["identities"]["owner"]]
                    assert enabled["search"]["embedder"] == actions["enable"]["body"]["embedder"]
                    assert enabled["search"]["vector_store_provider_id"] == ssp
                    status = await call("collections_api__get_collection_search_status", {"collection_id": collection})
                    assert status["state"] == "ready" and status["documents_indexed"] == 3, status
                    first = await call("collections__search", {"collection": collection, "query": "Cedar", "mode": "semantic", "limit": 5})
                    assert first["mode_used"] == "semantic" and any(h["path"] == "overview" and "Cedar" in h["excerpt"] for h in first["hits"]), first
                    trace["semantic_backfill"] = {"status": status, "result": first}
                    await stage("import")
                    reread = await call("system__get_document_content", {"collection_id": collection, "path": "guides/oncall"})
                    assert reread["content"] == text
                    policy = await call("system__get_document_content", {"collection_id": collection, "path": "policies/handbook"})
                    assert policy["content"] == "Policy copy\n" + text
                    tree = await call("collections__collection_tree", {"collection": collection, "depth": 3})
                    nodes = {n["path"]: n for n in tree["nodes"]}
                    assert all(nodes[p]["has_children"] is flag for p, flag in {
                        "guides": True, "guides/oncall": False, "policies": True, "policies/handbook": False,
                    }.items())
                    found = await call("collections__search", {"collection": collection, "query": "Cedar on-call escalation", "path_prefix": "guides", "mode": "semantic", "limit": 5})
                    assert found["mode_used"] == "semantic" and any(h["path"] == "guides/oncall" and unique_suffix in h["excerpt"] for h in found["hits"]), found
                    trace["semantic_import"] = {"reread": reread, "policy_reread": policy, "tree": tree, "result": found}
                    await stage("disable")
                    status = await call("collections_api__get_collection_search_status", {"collection_id": collection})
                    assert status["state"] == "disabled"
                    assert (await call("system__get_collection", {"id": collection}))["search"] is None
                    literal = await call("collections__search", {"collection": collection, "query": unique_suffix, "mode": "literal", "limit": 10})
                    assert literal["mode_used"] == "literal" and any(h["path"] == "guides/oncall" for h in literal["hits"])
                    await call("collections__search", {"collection": collection, "query": "Cedar", "mode": "semantic"}, error="tool_failed")
                    trace["disabled"] = {"status": status, "literal": literal}
                    # The collection is disposable; deletion is a fixture cleanup,
                    # never a default action in the ingestion runbook.
                    response = await owner.delete(f"/v1/collections/{collection}")
                    assert response.status_code == 204, response.text
                    assert (await owner.get(f"/v1/collections/{collection}")).status_code == 404
                    response = await owner.get("/v1/workspaces/" + workspaces["owner"])
                    assert response.status_code == 200 and response.json()["is_default"]
                    assert response.json()["access"] == default_before["access"]
                    assert (tmp_path / "workspaces" / workspaces["owner"]).is_dir()
                    assert (tmp_path / "vectors").is_dir()
                    assert script.requests and all(q["model"] == script.model for q in script.requests)
                    trace["embedding_requests"] = script.requests
                    trace["checks"].update(collection_deleted=True, default_preserved=True, private_vector_path=True)
                    trace["outcome"] = "passed"
                    require_evidence(trace, trace["source_sha"], hashes)
        finally:
            if process is not None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
            trace["embedding_requests"] = script.requests
            (evidence / "trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n")


def require_evidence(trace: dict, source_sha: str, runbook_hashes: dict) -> None:
    from primer.bootstrap.seed import default_policy_id
    from primer.model.chat import Message, ToolCallPart

    assert trace["case"] == "test_user_ingests_and_searches_collection_with_real_gates_and_access"
    assert trace["outcome"] == "passed" and trace["source_sha"] == source_sha
    assert trace["runbook_hashes"] == runbook_hashes
    identities = trace["identities"]
    assert set(identities) == {"owner", "peer", "reader"} and len(set(identities.values())) == 3
    assert trace["approver"] not in identities.values()
    assert set(trace["stages"]) == set(SPECS)
    assert [a["stage"] for a in trace["approvals"]] == list(SPECS)
    assert len({a["gate_id"] for a in trace["approvals"]}) == len(SPECS)
    search = trace["stages"]["enable"]["result"]["search"]
    assert search["embedder"]["provider_id"] and search["embedder"]["model"] == "w7-embedding"
    assert search["vector_store_provider_id"]
    actions = action_arguments(trace["collection"], search["embedder"]["provider_id"], search["vector_store_provider_id"], trace["text"])
    for name, (operation, role, decision) in SPECS.items():
        stage = trace["stages"][name]
        approval = next(a for a in trace["approvals"] if a["stage"] == name)
        assert stage["actor"] == role and stage["initiator"] == identities[role] and stage["completed"] is True
        assert stage["started"]["initiated_by"]["user_id"] == identities[role]
        assert approval["session_id"] == stage["session_id"] and approval["operation"] == operation
        assert approval["decision"] == decision and approval["decision_http"] == 202 and approval["effect_absent_before"] is True
        assert approval["pending"]["approval_type"] == "required" and approval["policy_id"]
        assert approval["pending"]["gate_id"] == approval["gate_id"]
        assert approval["pending"]["policy_id"] == approval["policy_id"]
        assert approval["pending"]["tool_name"] == operation
        assert approval["policy_id"] == default_policy_id("collections_api", operation)
        assert approval["pending"]["arguments"] == actions[name]
        assert stage["started"]["initiated_by"]["role"] == "user"
        assert stage["before"]["access"]["owners"] == stage["after"]["access"]["owners"] == [identities["owner"]]
        assert stage["before"]["access"]["grants"] == {identities["peer"]: "full", identities["reader"]: "read"}
        calls = [p for row in stage["items"] if "kind" not in row
                 for p in Message.model_validate(row).parts if isinstance(p, ToolCallPart)]
        assert len(calls) == 1 and calls[0].id == name and calls[0].name == "system__call_tool"
        assert calls[0].arguments == {"toolset_id": "collections_api", "tool_name": operation, "arguments": actions[name]}
        records, answer, result = read_stage(stage["items"], name)
        assert stage["records"] == records and stage["answer"] == answer and stage["result"] == result
        if name in {"reject_enable", "peer_enable", "reader_import"}:
            assert stage["before"] == stage["after"]
        elif name == "enable":
            assert stage["after"]["search"] == result["search"]
        elif name == "disable":
            assert stage["after"]["search"] is None
        elif name == "import":
            assert any(d["path"] == "guides/oncall" for d in stage["after"]["documents"])
    for key in ("semantic_backfill", "semantic_import"):
        result = trace[key]["result"]
        assert result["mode_used"] == "semantic" and result["hits"]
    assert trace["semantic_backfill"]["status"]["state"] == "ready"
    assert trace["semantic_backfill"]["status"]["documents_indexed"] == 3
    assert trace["needle"] and trace["needle"] in trace["text"]
    assert trace["semantic_import"]["reread"]["content"] == trace["text"]
    assert trace["semantic_import"]["policy_reread"]["content"] == "Policy copy\n" + trace["text"]
    nodes = {n["path"]: n for n in trace["semantic_import"]["tree"]["nodes"]}
    assert all(nodes[p]["has_children"] is flag for p, flag in {
        "guides": True, "guides/oncall": False, "policies": True, "policies/handbook": False,
    }.items())
    assert any(h["path"] == "guides/oncall" and trace["needle"] in h["excerpt"] for h in trace["semantic_import"]["result"]["hits"])
    assert trace["disabled"]["status"]["state"] == "disabled" and trace["disabled"]["literal"]["mode_used"] == "literal"
    assert set(trace["checks"]) == {"admin_provider_reads_refused", "unapproved_native_write_has_no_effect",
                                    "rejection_and_nonowner_have_no_effect", "collection_deleted", "default_preserved", "private_vector_path"}
    assert all(value is True for value in trace["checks"].values())
    assert trace["embedding_requests"] and all(q["model"] == "w7-embedding" for q in trace["embedding_requests"])
    embedded = [text for q in trace["embedding_requests"] for text in ([q["input"]] if isinstance(q["input"], str) else q["input"])]
    assert any(trace["needle"] in text for text in embedded)
    assert "Cedar on-call escalation" in embedded
    assert any(h["path"] == "guides/oncall" for h in trace["disabled"]["literal"]["hits"])
