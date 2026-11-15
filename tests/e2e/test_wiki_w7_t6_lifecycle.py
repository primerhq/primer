"""CI-owned private harness + artifact lifecycle; collection starts nothing.

Only model responses are scripted. Git, rendering, approvals, MCP, worker,
SQLite and byte routing use production paths. File-Git is enabled only in
the disposable child; no shared API, exposure, mounts or provider is touched.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for
from tests.e2e.test_wiki_w7_t1_lifecycle import _share, isolated_config, mcp_http_client
from tests.e2e.test_wiki_w7_t2_lifecycle import stage_answer

REPO = Path(__file__).resolve().parents[2]
PAGES = ("runbooks/install-a-harness", "runbooks/configure-artifact-storage")
CASE = "test_admin_installs_syncs_uninstalls_bundle_and_proves_artifact_byte_routing"
SPECS = {"reject_install": ("install", "rejected"), "install": ("install", "approved"),
         "sync": ("sync", "approved"), "uninstall": ("uninstall", "approved")}
HARNESS = {"harness__" + n for n in ("register", "fetch", "update", "update_overrides", "install", "sync", "uninstall", "get", "list")}
ARTIFACT = {"system__" + n for n in ("create_artifact_storage_provider", "test_artifact_storage_provider", "set_active_artifact_store",
             "get_active_artifact_store", "list_artifact_storage_providers", "get_artifact_storage_provider", "delete_artifact_storage_provider")}
ALLOWED = HARNESS | ARTIFACT | {"system__wiki_read", "system__describe_tool", "system__get_agent", "system__get_graph"}


def page_hashes() -> dict:
    return {p: hashlib.sha256((REPO / "docs/wiki" / (p + ".md")).read_bytes()).hexdigest() for p in PAGES}


def bundle_files() -> dict[str, str]:
    """One real runnable graph, referring to the bundle's own agent."""
    return {
        "harness.yaml": "apiVersion: primer/v1\nkind: Harness\nname: Private T6 bundle\n",
        "overrides.schema.json": json.dumps({"type": "object", "additionalProperties": False,
            "required": ["profile_id", "description"], "properties": {"profile_id": {"type": "string", "minLength": 1},
            "description": {"type": "string", "minLength": 1}}}),
        "templates/assistant.yaml": 'kind: agent\nname: assistant\nspec:\n  description: "{{ overrides.description }}"\n  model:\n    profile_id: "{{ overrides.profile_id }}"\n  tools: []\n',
        "templates/workflow.yaml": 'kind: graph\nname: workflow\nspec:\n  description: "{{ overrides.description }}"\n  nodes:\n    - {kind: begin, id: begin}\n    - {kind: agent, id: answer, agent_id: assistant}\n    - {kind: end, id: end}\n  edges:\n    - {kind: static, from_node: begin, to_node: answer}\n    - {kind: static, from_node: answer, to_node: end}\n',
    }


def make_git_bundle(root: Path) -> tuple[str, str]:
    work, remote = root / "git-source", root / "bundle.git"
    work.mkdir()
    for name, body in bundle_files().items():
        path = work / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_text(body)
    def git(*args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
            cwd=work, env=private_env(), check=True, capture_output=True, text=True, timeout=15).stdout.strip()
    git("init", "-b", "main")
    git("-c", "user.name=Private T6", "-c", "user.email=t6@example.invalid", "add", "--", *bundle_files())
    git("-c", "user.name=Private T6", "-c", "user.email=t6@example.invalid", "commit", "-m", "Private acceptance bundle")
    commit = git("rev-parse", "HEAD")
    git("clone", "--bare", "--", str(work), str(remote))
    return remote.as_uri(), commit


def private_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRIMER_", "GIT_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


def prepare_config(root: Path, port: int) -> tuple[Path, dict]:
    from primer.model.harness import FILE_GIT_URLS_ENV

    wiki = root / "wiki";(wiki / "runbooks").mkdir(parents=True)
    (wiki / "index.md").write_text("---\ntitle: T6 acceptance\nsummary: Private storage flow.\n---\n[Runbooks](runbooks)\n")
    (wiki / "runbooks/index.md").write_text("---\ntitle: T6 runbooks\nsummary: Private storage flow.\n---\n" +
        "\n".join("[Runbook](" + p + ")" for p in PAGES))
    for page in PAGES:shutil.copyfile(REPO / "docs/wiki" / (page + ".md"), wiki / (page + ".md"))
    (root / "legacy-empty").mkdir()
    config = root / "config.json";config.write_text(json.dumps(isolated_config(root, port)))
    env = private_env()
    env.update(PRIMER_WIKI_DIR=str(wiki), PRIMER_AI_DOCS_DIR=str(root / "legacy-empty"))
    env[FILE_GIT_URLS_ENV] = "1"
    return config, env


def start_server(root: Path, port: int, log):
    config, env = prepare_config(root, port)
    return subprocess.Popen([sys.executable, "-m", "primer", "api", "--config", str(config)],
                            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


async def stop_server(process):
    # The new process group contains only this API and its own Git children.
    # Always stop the group, even if the API has already exited; no orphaned
    # Git command may retain the private temporary source beyond the case.
    try:os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:pass
    try:await asyncio.to_thread(process.wait, timeout=10)
    except subprocess.TimeoutExpired:
        try:os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:pass
        await asyncio.to_thread(process.wait, timeout=5)
    finally:
        try:os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:pass


def gate_result(name, value):
    operation, decision = SPECS[name]
    if decision == "rejected":
        assert value["rejected"] is True and value["tool_name"] == operation
    else:
        assert value["id"] and value["pending_operation"] == operation and value["last_operation_error"] is None


class GateRule(Rule):
    def __init__(self, name, arguments):
        super().__init__();self.name, self.arguments = name, arguments

    def matches(self, request):
        if not any(m.get("role") == "user" and m.get("content") == self.name for m in request.get("messages", [])):return False
        last = next((m for m in reversed(request["messages"]) if m.get("role") == "tool"), {})
        if last.get("tool_call_id") == self.name:
            value = json.loads(last["content"]);gate_result(self.name, value)
            self.emit_tool, self.emit_text = None, "RESULT:" + json.dumps(value)
        else:
            self.emit_text = None;self.emit_tool, self.emit_tool_call_id = "system__call_tool", self.name
            self.emit_args = {"toolset_id": "harness", "tool_name": SPECS[self.name][0], "arguments": self.arguments}
        return True


def read_gate(items, name):
    from primer.model.chat import Message, ToolCallPart, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord

    rejected = SPECS[name][1] == "rejected"
    records, raw, calls = [], [], []
    for row in items:
        if "kind" in row:records.append(SessionMessageRecord.model_validate(row).model_dump(mode="json"))
        else:
            for part in Message.model_validate(row).parts:
                if isinstance(part, ToolResultPart):raw.append(part)
                if isinstance(part, ToolCallPart):calls.append(part)
    assert len(raw) == len(calls) == 1
    assert raw[0].id == calls[0].id == name and raw[0].error is rejected and calls[0].name == "system__call_tool"
    failed = [r for r in records if r["kind"] == "tool_result" and r["payload"].get("error")]
    assert len(failed) == int(rejected)
    if failed:assert failed[0]["payload"]["call_id"] == name and json.loads(failed[0]["payload"]["output"]) == json.loads(raw[0].output)
    answer, value = stage_answer(records)
    assert value == json.loads(raw[0].output);gate_result(name, value)
    return records, answer, value, calls[0].arguments


def tar_bytes(content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("index.html");info.size = len(content);tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def blobs(root):
    return sorted(p for p in root.rglob("artifact-*") if p.is_file() and not p.name.endswith(".json"))


@pytest.mark.asyncio
async def test_admin_installs_syncs_uninstalls_bundle_and_proves_artifact_byte_routing(mock_llm, unique_suffix, tmp_path, unused_tcp_port):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from primer.artifact.filesystem import ROOT_MARKER
    from primer.model.providers.artifact import DEFAULT_ARTIFACT_PROVIDER_ID

    registry, llm_url = mock_llm
    llm, model, driver = "w7-llm", "w7-t6-" + unique_suffix, "w7-driver"
    profile, slug = profile_id_for(llm, model), "w7-t6-" + unique_suffix
    actions = {name: {} for name in SPECS}
    registry.register(model, [GateRule(name, actions[name]) for name in SPECS])
    evidence = REPO / "tests/.e2e/w7-t6";evidence.mkdir(parents=True, exist_ok=True)
    trace = {"case": CASE, "source_sha": os.environ.get("GITHUB_SHA", "local"), "runbook_hashes": page_hashes(),
             "outcome": "running", "calls": [], "gates": [], "stages": {}, "actions": actions, "slug": slug}
    process = None
    try:
        url, commit = make_git_bundle(tmp_path);trace["git"] = {"url": url, "commit": commit}
        with (evidence / "server.stdout").open("wb") as log:
            process = start_server(tmp_path, unused_tcp_port, log)
            base = f"http://127.0.0.1:{unused_tcp_port}"
            async with AsyncExitStack() as stack:
                admin = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=10))
                ready = asyncio.get_running_loop().time() + 45
                while asyncio.get_running_loop().time() < ready:
                    assert process.poll() is None, (evidence / "server.stdout").read_text()[-6000:]
                    try:
                        if (await admin.get("/v1/health")).status_code == 200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.2)
                else:pytest.fail("private T6 child never became healthy")
                for verb in ("register", "login"):
                    response = await admin.post("/v1/auth/" + verb, json={"username": "w7-approver", "password": "private-w7-password-123"})
                    assert response.status_code in (200, 201), response.text
                status = (await admin.get("/v1/auth/status")).json();assert status["role"] == "admin"
                trace["approver"] = status["principal"]["user_id"]
                setup = [
                    ("workspace_providers", {"id": "w7-local", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path / "workspaces")}}),
                    ("workspace_templates", {"id": "w7-template", "description": "T6 private", "provider_id": "w7-local", "backend": {"kind": "local"}}),
                    ("llm_providers", {"id": llm, "provider": "openchat", "config": {"url": llm_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 2}}),
                    ("model_profiles", {"id": profile, "description": "T6 scripted model", "provider_id": llm, "model_name": model, "context_length": 32000}),
                    ("agents", {"id": driver, "description": "T6 approval driver", "model": {"profile_id": profile}, "tools": ["system__call_tool"], "max_tool_turns": 8}),
                ]
                for plural, body in setup:
                    response = await admin.post("/v1/" + plural, json=body);assert response.status_code == 201, response.text
                    await _share(admin, plural, body["id"])
                assert (await admin.post("/v1/setup/seed")).status_code == 200
                clients = {"approver": admin};trace["identities"] = {};defaults = {}
                for label, role in (("runner", "admin"), ("reader", "user"), ("stranger", "user")):
                    response = await admin.post("/v1/admin/users", json={"username": "w7-" + label, "password": "private-w7-user-password-123", "role": role})
                    assert response.status_code == 201, response.text
                    client = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=10))
                    response = await client.post("/v1/auth/login", json={"username": "w7-" + label, "password": "private-w7-user-password-123"})
                    assert response.status_code == 200, response.text
                    status = (await client.get("/v1/auth/status")).json();assert status["role"] == role
                    trace["identities"][label] = status["principal"]["user_id"]
                    clients[label], defaults[label] = client, status["default_workspace_id"]
                runner = clients["runner"]
                default_before = (await runner.get("/v1/workspaces/" + defaults["runner"])).json();assert default_before["is_default"]
                assert (await admin.put("/v1/mcp_exposure", json={"enabled": True, "mode": "allowlist", "allowed_tools": sorted(ALLOWED)})).status_code in (200, 204)
                sessions = {}
                for label, client in clients.items():
                    http = mcp_http_client("; ".join(f"{k}={v}" for k, v in client.cookies.items()));await stack.enter_async_context(http)
                    read, write = await stack.enter_async_context(streamable_http_client(base + "/v1/mcp/", http_client=http))
                    sdk = await stack.enter_async_context(ClientSession(read, write));await sdk.initialize()
                    assert {t.name for t in (await sdk.list_tools()).tools} == {"list_tools", "find_tools", "call_tool"};sessions[label] = sdk

                async def call(scoped, args, *, actor="runner", error=None):
                    result = await sessions[actor].call_tool("call_tool", arguments={"scoped_id": scoped, "arguments": args})
                    envelope = json.loads("".join(p.text for p in result.content if hasattr(p, "text")))
                    trace["calls"].append({"actor": actor, "scoped_id": scoped, "arguments": args, "envelope": envelope})
                    if error is not None:
                        assert envelope["ok"] is False and envelope["error"]["code"] == error, envelope
                        return envelope
                    assert not result.is_error and envelope["ok"] is True, envelope
                    return json.loads(envelope["output"])

                trace["wiki"] = {p: await call("system__wiki_read", {"path": p}) for p in PAGES}
                policies = {}
                for operation in ("install", "sync", "uninstall"):
                    detail = await call("system__describe_tool", {"toolset_id": "harness", "tool_name": operation})
                    assert detail["required_role"] == "admin" and detail["approval"]["type"] == "required" and detail["approval"]["gated"]
                    policies[operation] = detail["approval"]["policy_id"]
                registration = {"name": "Private T6", "slug": slug, "git_url": url, "ref": "main"}
                await call("harness__register", registration, actor="reader", error="tool_unavailable")
                registered = await call("harness__register", registration);identifier = registered["id"]
                assert registered["status"] == "draft" and registered["access"]["owners"] == [trace["identities"]["runner"]]
                trace["registered"] = registered
                hidden = await call("harness__get", {"id": identifier}, actor="stranger", error="tool_failed");trace["hidden"] = hidden
                path = "/v1/harnesses/" + identifier + "/access"
                access = (await runner.get(path)).json()
                response = await runner.put(path, json={"owners": [trace["identities"]["runner"]], "grants": {trace["identities"]["reader"]: "read"}, "public": False, "rev": access["rev"]})
                assert response.status_code == 200, response.text
                assert (await call("harness__get", {"id": identifier}, actor="reader"))["id"] == identifier
                before = await call("harness__get", {"id": identifier})
                await call("harness__fetch", {"id": identifier}, actor="reader", error="tool_unavailable")
                after_reader = await call("harness__get", {"id": identifier});assert after_reader == before
                trace["reader_no_effect"] = {"before": before, "after": after_reader}
                no_schema = await call("harness__update_overrides", {"id": identifier, "overrides": {}}, error="tool_failed")
                assert "overrides-schema-missing" in no_schema["error"]["message"]
                trace["no_schema"] = no_schema
                trace["fetch_queued"] = await call("harness__fetch", {"id": identifier})
                deadline = asyncio.get_running_loop().time() + 120

                async def settled(expected):
                    while asyncio.get_running_loop().time() < deadline:
                        row = await call("harness__get", {"id": identifier})
                        assert row["last_operation_error"] is None, row
                        if row["pending_operation"] is None:
                            assert row["status"] == expected;return row
                        await asyncio.sleep(.1)
                    pytest.fail("harness operation never settled: " + expected)

                fetched = await settled("ready");trace["fetched"] = fetched
                assert fetched["available_commit"] == commit and fetched["overrides_schema"]
                invalid = await call("harness__update_overrides", {"id": identifier, "overrides": {}}, error="tool_failed")
                assert "overrides-invalid" in invalid["error"]["message"]
                trace["invalid_overrides"] = invalid
                overrides = {"profile_id": profile, "description": "Private bundle v1 " + unique_suffix}
                saved = await call("harness__update_overrides", {"id": identifier, "overrides": overrides})
                assert saved["overrides"] == overrides
                for name in ("reject_install", "install"):actions[name].update(id=identifier, overrides=overrides.copy())
                actions["uninstall"]["id"] = identifier
                await call("harness__install", actions["install"], error="tool_unavailable")

                async def stage(name):
                    operation, decision = SPECS[name];before = await call("harness__get", {"id": identifier})
                    response = await runner.post("/v1/workspaces/" + defaults["runner"] + "/sessions", json={
                        "binding": {"kind": "agent", "agent_id": driver}, "initial_instructions": name, "auto_start": True, "autonomous": True})
                    assert response.status_code == 201, response.text
                    started = response.json();sid = started["id"]
                    assert started["initiated_by"]["user_id"] == trace["identities"]["runner"] and started["initiated_by"]["role"] == "admin"
                    while asyncio.get_running_loop().time() < deadline:
                        response = await admin.get(f"/v1/sessions/{sid}/tool_approval/pending")
                        if response.status_code == 200:pending = response.json();break
                        assert response.status_code == 404, response.text
                        await asyncio.sleep(.1)
                    else:pytest.fail("harness gate never parked: " + name)
                    assert pending["tool_name"] == operation and pending["approval_type"] == "required"
                    assert pending["approvers"] == {"kind": "roles", "roles": [], "users": []}
                    assert pending["policy_id"] == policies[operation] and pending["arguments"] == actions[name] and pending["gate_id"]
                    assert await call("harness__get", {"id": identifier}) == before
                    body = {"tool_call_id": pending["tool_call_id"], "gate_id": pending["gate_id"], "decision": decision, "reason": "Private T6 " + name}
                    refused = await clients["reader"].post(f"/v1/sessions/{sid}/tool_approval/respond", json=body)
                    assert refused.status_code == 403 and "approver_mismatch" in refused.text, refused.text
                    assert (await admin.get(f"/v1/sessions/{sid}/tool_approval/pending")).json()["gate_id"] == pending["gate_id"]
                    response = await admin.post(f"/v1/sessions/{sid}/tool_approval/respond", json=body)
                    assert response.status_code == 202 and response.json() == {"status": "accepted"}, response.text
                    while asyncio.get_running_loop().time() < deadline:
                        row = (await runner.get(f"/v1/sessions/{sid}")).json()
                        assert row["ended_reason"] not in {"failed", "cancelled"}, row
                        if row["turn_status"] == "idle" and row["ended_reason"] == "completed":break
                        await asyncio.sleep(.1)
                    else:pytest.fail("harness gate never completed: " + name)
                    items = (await runner.get(f"/v1/sessions/{sid}/messages", params={"limit": 200})).json()["items"]
                    records, answer, value, actual_args = read_gate(items, name)
                    assert actual_args == {"toolset_id": "harness", "tool_name": operation, "arguments": actions[name]}
                    trace["gates"].append({"stage": name, "operation": operation, "decision": decision, "http": 202,
                        "policy_id": policies[operation], "gate_id": pending["gate_id"], "pending": pending,
                        "reader_http": refused.status_code, "reader_body": refused.json()})
                    trace["stages"][name] = {"started": started, "row": row, "items": items, "records": records, "answer": answer, "result": value, "before": before}
                    return value

                await stage("reject_install")
                trace["rejected_after"] = await call("harness__get", {"id": identifier})
                assert trace["rejected_after"] == trace["stages"]["reject_install"]["before"]
                trace["rejection_missing"] = {"agent": (await runner.get("/v1/agents/" + slug + "__assistant")).status_code,
                    "graph": (await runner.get("/v1/graphs/" + slug + "__workflow")).status_code}
                assert trace["rejection_missing"] == {"agent": 404, "graph": 404}
                await stage("install");trace["installed"] = await settled("installed")

                async def entities():
                    return {"agent": await call("system__get_agent", {"id": slug + "__assistant"}),
                            "graph": await call("system__get_graph", {"id": slug + "__workflow"})}
                trace["entities_v1"] = await entities()
                overrides = {**overrides, "description": "Private bundle v2 " + unique_suffix}
                await call("harness__update", {"id": identifier, "description": "T6 source reviewed"})
                await call("harness__update_overrides", {"id": identifier, "overrides": overrides})
                actions["sync"].update(id=identifier, overrides=overrides.copy())
                await stage("sync");trace["synced"] = await settled("installed");trace["entities_v2"] = await entities()
                await stage("uninstall")
                while asyncio.get_running_loop().time() < deadline:
                    response = await runner.get("/v1/harnesses/" + identifier)
                    if response.status_code == 404:break
                    assert response.status_code == 200 and response.json()["last_operation_error"] is None, response.text
                    await asyncio.sleep(.1)
                else:pytest.fail("uninstall never removed harness")
                trace["uninstalled"] = {"harness": response.status_code,
                    "agent": (await runner.get("/v1/agents/" + slug + "__assistant")).status_code,
                    "graph": (await runner.get("/v1/graphs/" + slug + "__workflow")).status_code,
                    "list": await call("harness__list", {"slug": slug})}

                original = await call("system__get_active_artifact_store", {})
                assert original == {"provider_id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "db"}
                default_provider = await call("system__get_artifact_storage_provider", {"id": DEFAULT_ARTIFACT_PROVIDER_ID})
                fs_root = tmp_path / "artifacts";provider = "w7-artifacts-" + unique_suffix
                entity = {"id": provider, "provider": "filesystem", "config": {"root": str(fs_root)}}
                for scoped, args in (("system__create_artifact_storage_provider", {"entity": entity}),
                        ("system__set_active_artifact_store", {"provider_id": provider}), ("system__get_active_artifact_store", {})):
                    await call(scoped, args, actor="reader", error="tool_unavailable")
                assert await call("system__get_active_artifact_store", {}) == original and not fs_root.exists()
                missing = await call("system__set_active_artifact_store", {"provider_id": "not-present"}, error="tool_failed")
                assert await call("system__get_active_artifact_store", {}) == original
                response = await runner.post("/v1/services", json={"name": "t6-" + unique_suffix, "description": "Private byte routing"})
                assert response.status_code == 201, response.text
                service = response.json();content = ["DB bytes " + unique_suffix, "FS bytes " + unique_suffix]
                async def publish(body):
                    response = await runner.post("/v1/services/" + service["id"] + "/versions", content=tar_bytes(body.encode()), headers={"content-type": "application/gzip"})
                    assert response.status_code == 201, response.text
                    return response.json()
                v1 = await publish(content[0])
                created = await call("system__create_artifact_storage_provider", {"entity": entity})
                assert (fs_root / ROOT_MARKER).is_file()
                probe = await call("system__test_artifact_storage_provider", {"id": provider});assert probe == {"ok": True}
                assert await call("system__get_artifact_storage_provider", {"id": provider}) == created
                listed = await call("system__list_artifact_storage_providers", {"limit": 200})
                assert provider in {i["id"] for i in listed["items"]}
                active = await call("system__set_active_artifact_store", {"provider_id": provider})
                assert active == await call("system__get_active_artifact_store", {}) == {"provider_id": provider, "provider": "filesystem"}
                v2 = await publish(content[1]);stored = blobs(fs_root)
                assert len(stored) == 1 and stored[0].read_bytes() == content[1].encode()
                stored_hash = hashlib.sha256(stored[0].read_bytes()).hexdigest()
                response = await runner.post("/v1/services/" + service["id"] + "/_activate", json={"version_id": v1["id"]})
                assert response.status_code == 200, response.text
                old_read = await runner.get("/svc/" + service["name"] + "/")
                assert old_read.status_code == 200 and old_read.content == content[0].encode(), old_read.text
                old_on_fs = {"http": old_read.status_code, "content": old_read.text, "active": await call("system__get_active_artifact_store", {})}
                blocked_active = await runner.delete("/v1/artifact_storage_providers/" + provider)
                assert blocked_active.status_code == 409 and blocked_active.json()["extensions"]["error"] == "cascade_blocked", blocked_active.text
                assert await call("system__set_active_artifact_store", {"provider_id": original["provider_id"]}) == original
                blocked_held = await runner.delete("/v1/artifact_storage_providers/" + provider)
                assert blocked_held.status_code == 409 and blocked_held.json()["extensions"]["error"] == "artifact_provider_holds_artifacts", blocked_held.text
                moved_root = tmp_path / "must-not-create"
                moved = await runner.put("/v1/artifact_storage_providers/" + provider, json={**entity, "config": {"root": str(moved_root)}})
                assert moved.status_code == 409 and moved.json()["extensions"]["error"] == "artifact_provider_holds_artifacts", moved.text
                assert not moved_root.exists()
                reads = []
                for version, expected in ((v2, content[1]), (v1, content[0])):
                    response = await runner.post("/v1/services/" + service["id"] + "/_activate", json={"version_id": version["id"]})
                    assert response.status_code == 200, response.text
                    response = await runner.get("/svc/" + service["name"] + "/")
                    assert response.status_code == 200 and response.content == expected.encode(), response.text
                    reads.append({"version": version, "http": response.status_code, "content": response.text, "sha256": hashlib.sha256(response.content).hexdigest()})
                response = await runner.delete("/v1/services/" + service["id"]);assert response.status_code == 204, response.text
                assert blobs(fs_root) == []
                deleted = await call("system__delete_artifact_storage_provider", {"id": provider});assert deleted == {"deleted": True, "id": provider}
                provider_missing = await call("system__get_artifact_storage_provider", {"id": provider}, error="tool_failed")
                assert await call("system__get_active_artifact_store", {}) == original
                default_after = await call("system__get_artifact_storage_provider", {"id": DEFAULT_ARTIFACT_PROVIDER_ID})
                assert default_after == default_provider
                trace["artifact"] = {"original": original, "default_before": default_provider, "default_after": default_after,
                    "created": created, "probe": probe, "active": active, "missing_selection": missing, "versions": [v1, v2],
                    "contents": content, "reads": reads, "stored_sha256": stored_hash, "old_on_fs": old_on_fs,
                    "blocked_active": blocked_active.json(), "blocked_held": blocked_held.json(), "blocked_move": moved.json(),
                    "moved_root_exists": moved_root.exists(), "service_delete": response.status_code, "blobs_after": [],
                    "provider_deleted": deleted, "provider_missing": provider_missing, "restored": await call("system__get_active_artifact_store", {})}
                after = (await runner.get("/v1/workspaces/" + defaults["runner"])).json()
                trace["default"] = {"before": default_before, "after": after, "directory_exists": (tmp_path / "workspaces" / defaults["runner"]).is_dir()}
                trace["outcome"] = "passed";require_evidence(trace, trace["source_sha"], page_hashes())
    finally:
        try:
            if process is not None:await stop_server(process)
        finally:
            (evidence / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")


def require_evidence(trace, source, hashes):
    from primer.bootstrap.seed import default_policy_id
    from primer.model.agent import Agent
    from primer.model.graph import Graph
    from primer.model.providers.artifact import DEFAULT_ARTIFACT_PROVIDER_ID

    assert trace["case"] == CASE and trace["outcome"] == "passed"
    assert trace["source_sha"] == source and trace["runbook_hashes"] == hashes and set(hashes) == set(PAGES)
    def native(scoped, args, value):
        matches = [c for c in trace["calls"] if c["actor"] == "runner" and c["scoped_id"] == scoped and
                   c["arguments"] == args and c["envelope"]["ok"] is True]
        assert any(json.loads(c["envelope"]["output"]) == value for c in matches), scoped
    for p in PAGES:
        assert trace["wiki"][p]["path"] == p
        assert (REPO / "docs/wiki" / (p + ".md")).read_text().split("---", 2)[2].strip() in trace["wiki"][p]["body"]
        native("system__wiki_read", {"path": p}, trace["wiki"][p])
    ids = trace["identities"]
    assert set(ids) == {"runner", "reader", "stranger"} and len(set(ids.values()) | {trace["approver"]}) == 4
    registered = trace["registered"];identifier = registered["id"]
    assert registered["status"] == "draft" and registered["slug"] == trace["slug"] and registered["access"]["owners"] == [ids["runner"]]
    assert registered["git_url"] == trace["git"]["url"] and trace["git"]["url"].startswith("file:///")
    assert len(trace["git"]["commit"]) == 40
    assert trace["hidden"]["ok"] is False and "not-found" in trace["hidden"]["error"]["message"]
    assert trace["reader_no_effect"]["before"] == trace["reader_no_effect"]["after"]
    assert trace["no_schema"]["ok"] is False and "overrides-schema-missing" in trace["no_schema"]["error"]["message"]
    assert trace["invalid_overrides"]["ok"] is False and "overrides-invalid" in trace["invalid_overrides"]["error"]["message"]
    assert trace["fetch_queued"]["pending_operation"] == "fetch"
    fetched = trace["fetched"]
    assert fetched["status"] == "ready" and fetched["pending_operation"] is None and fetched["last_operation_error"] is None
    assert fetched["available_commit"] == trace["git"]["commit"] and fetched["overrides_schema"]
    native("harness__get", {"id": identifier}, fetched)
    assert set(trace["stages"]) == set(SPECS) and [g["stage"] for g in trace["gates"]] == list(SPECS)
    assert len({g["gate_id"] for g in trace["gates"]}) == 4
    for gate in trace["gates"]:
        name = gate["stage"];operation, decision = SPECS[name];stage = trace["stages"][name]
        assert gate["operation"] == operation and gate["decision"] == decision and gate["http"] == 202
        assert gate["policy_id"] == gate["pending"]["policy_id"] == default_policy_id("harness", operation)
        assert gate["gate_id"] == gate["pending"]["gate_id"] and gate["pending"]["approval_type"] == "required"
        assert gate["pending"]["approvers"] == {"kind": "roles", "roles": [], "users": []}
        assert gate["pending"]["tool_name"] == operation and gate["pending"]["arguments"] == trace["actions"][name]
        assert gate["reader_http"] == 403 and "approver_mismatch" in json.dumps(gate["reader_body"])
        for key in ("started", "row"):
            assert stage[key]["initiated_by"]["user_id"] == ids["runner"] and stage[key]["initiated_by"]["role"] == "admin"
        assert stage["row"]["ended_reason"] == "completed" and stage["row"]["turn_status"] == "idle"
        records, answer, value, arguments = read_gate(stage["items"], name)
        assert records == stage["records"] and answer == stage["answer"] and value == stage["result"]
        assert arguments == {"toolset_id": "harness", "tool_name": operation, "arguments": trace["actions"][name]}
        assert trace["actions"][name]["id"] == identifier
    assert trace["rejected_after"] == trace["stages"]["reject_install"]["before"]
    assert trace["rejection_missing"] == {"agent": 404, "graph": 404}
    for version, state, action in (("v1", "installed", "install"), ("v2", "synced", "sync")):
        row = trace[state];values = trace["actions"][action]["overrides"]
        assert row["id"] == identifier and row["status"] == "installed" and row["pending_operation"] is None and row["last_operation_error"] is None
        assert row["resolved_commit"] == trace["git"]["commit"] and row["overrides"] == values
        entities = trace["entities_" + version]
        agent, graph = Agent.model_validate(entities["agent"]), Graph.model_validate(entities["graph"])
        assert agent.id == trace["slug"] + "__assistant" and graph.id == trace["slug"] + "__workflow"
        assert agent.harness_id == graph.harness_id == identifier and agent.description == graph.description == values["description"]
        assert agent.model.profile_id == values["profile_id"]
        assert [n.agent_id for n in graph.nodes if n.kind == "agent"] == [agent.id]
        graph.assert_runnable()
        native("system__get_agent", {"id": agent.id}, entities["agent"])
        native("system__get_graph", {"id": graph.id}, entities["graph"])
        native("harness__get", {"id": identifier}, row)
    assert trace["actions"]["install"]["overrides"]["description"] != trace["actions"]["sync"]["overrides"]["description"]
    assert trace["uninstalled"]["harness"] == trace["uninstalled"]["agent"] == trace["uninstalled"]["graph"] == 404
    listed = trace["uninstalled"]["list"]
    assert identifier not in {i["id"] for i in listed["items"]}
    calls = trace["calls"]
    refused = [c for c in calls if c["actor"] == "reader"]
    assert {c["scoped_id"] for c in refused} == {"harness__register", "harness__get", "harness__fetch", "system__create_artifact_storage_provider", "system__set_active_artifact_store", "system__get_active_artifact_store"}
    assert all(c["envelope"]["ok"] is (c["scoped_id"] == "harness__get") for c in refused)
    artifact = trace["artifact"]
    assert artifact["original"] == artifact["restored"] == {"provider_id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "db"}
    assert artifact["default_before"] == artifact["default_after"] and artifact["default_before"]["id"] == DEFAULT_ARTIFACT_PROVIDER_ID
    assert artifact["default_before"]["provider"] == "db"
    assert artifact["created"]["provider"] == "filesystem" and artifact["probe"] == {"ok": True}
    assert artifact["active"] == {"provider_id": artifact["created"]["id"], "provider": "filesystem"}
    native("system__create_artifact_storage_provider", {"entity": artifact["created"]}, artifact["created"])
    native("system__test_artifact_storage_provider", {"id": artifact["created"]["id"]}, artifact["probe"])
    native("system__set_active_artifact_store", {"provider_id": artifact["created"]["id"]}, artifact["active"])
    native("system__set_active_artifact_store", {"provider_id": DEFAULT_ARTIFACT_PROVIDER_ID}, artifact["restored"])
    native("system__get_active_artifact_store", {}, artifact["original"])
    native("system__get_artifact_storage_provider", {"id": DEFAULT_ARTIFACT_PROVIDER_ID}, artifact["default_after"])
    native("system__delete_artifact_storage_provider", {"id": artifact["created"]["id"]}, artifact["provider_deleted"])
    assert artifact["missing_selection"]["ok"] is False
    assert artifact["old_on_fs"] == {"http": 200, "content": artifact["contents"][0], "active": artifact["active"]}
    assert len(artifact["versions"]) == len(artifact["contents"]) == len(artifact["reads"]) == 2
    for index, read in zip((1, 0), artifact["reads"], strict=True):
        assert read["version"] == artifact["versions"][index] and read["http"] == 200 and read["content"] == artifact["contents"][index]
        assert read["sha256"] == hashlib.sha256(read["content"].encode()).hexdigest()
    assert artifact["stored_sha256"] == hashlib.sha256(artifact["contents"][1].encode()).hexdigest()
    assert artifact["blocked_active"]["extensions"]["error"] == "cascade_blocked"
    for key in ("blocked_held", "blocked_move"):
        assert artifact[key]["extensions"]["error"] == "artifact_provider_holds_artifacts" and artifact[key]["extensions"]["count"] == 1
    assert artifact["moved_root_exists"] is False and artifact["service_delete"] == 204 and artifact["blobs_after"] == []
    assert artifact["provider_deleted"] == {"deleted": True, "id": artifact["created"]["id"]} and artifact["provider_missing"]["ok"] is False
    default = trace["default"]
    assert default["before"]["id"] == default["after"]["id"] and default["before"]["is_default"] is default["after"]["is_default"] is True
    assert default["before"]["access"] == default["after"]["access"] and default["directory_exists"] is True
