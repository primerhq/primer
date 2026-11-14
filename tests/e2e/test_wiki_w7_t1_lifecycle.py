"""CI-owned W7 T1 acceptance, on a disposable authenticated server.

Only model responses are scripted. MCP, access, SQLite, workspace files,
worker execution, wiki reads and destruction use production paths. No shared
server/exposure state is changed. Collection starts no process or network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for

REPO = Path(__file__).resolve().parents[2]
PAGE = "runbooks/manage-a-workspace"
WRITES = ("create_workspace", "write_workspace_file", "create_workspace_session", "delete_workspace")
ALLOWED = {
    "system__wiki_read", "system__describe_tool", "system__get_agent", "system__list_model_profile_choices",
    *["workspaces__" + name for name in (
        *WRITES, "list_workspace_templates", "get_workspace_template", "list_workspaces", "get_workspace",
        "list_workspace_files", "read_workspace_file", "get_workspace_log", "get_workspace_session",
        "read_workspace_session_messages",
    )],
}


def isolated_config(root: Path, port: int) -> dict:
    return {
        "host": "127.0.0.1", "port": port,
        "db": {"provider": "sqlite", "config": {"path": str(root / "data.sqlite")}},
        "auto_bootstrap": False, "default_workspace_template": "w7-template",
        "auth": {"enabled": True},
        "scheduler": {"provider": "in_memory", "config": {}},
        "worker": {"concurrency": 2, "poll_interval_seconds": 0.1, "drain_timeout_seconds": 3},
        "log_level": "warning",
    }


class FileAnswerRule(Rule):
    """An answer exists only after a successful actual file-tool result.

    Never put the expected bytes in the prompt or this rule's constructor.
    The session must execute the read; the next model request supplies the
    contents from which the scripted model derives its answer.
    """

    def matches(self, request: dict) -> bool:
        last = next((m for m in reversed(request.get("messages", [])) if m.get("role") == "tool"), {})
        if last.get("tool_call_id") != "w7_file_read":
            return False
        value = json.loads(last["content"])
        assert value["path"] == "notes/welcome.txt" and value["encoding"] == "text", value
        assert isinstance(value["content"], str) and value["content"], value
        assert value["size_bytes"] == len(value["content"].encode()), value
        self.emit_text = "FILE-ANSWER:" + value["content"]
        return True


class AfterWikiRule(Rule):
    def matches(self, request: dict) -> bool:
        last = next((m for m in reversed(request.get("messages", [])) if m.get("role") == "tool"), {})
        return last.get("tool_call_id") == "w7_wiki_read" and super().matches(request)


def assistant_text(records: list[dict]) -> str:
    return "".join(row["payload"]["text"] for row in records if row.get("kind") == "assistant_token")


def mcp_http_client(cookie: str):
    # The SDK's transport is httpx2; httpx.Timeout is a foreign object that
    # httpx2 treats as a scalar, failing before its first connection.
    import httpx2
    from mcp.shared._httpx_utils import create_mcp_http_client

    return create_mcp_http_client(headers={"Cookie": cookie}, timeout=httpx2.Timeout(15))


async def _share(admin: httpx.AsyncClient, plural: str, entity_id: str) -> None:
    response = await admin.get(f"/v1/{plural}/{entity_id}/access")
    assert response.status_code == 200, response.text
    view = response.json()
    response = await admin.put(f"/v1/{plural}/{entity_id}/access", json={
        "owners": [owner["id"] for owner in view["owners"]],
        "grants": {}, "public": True, "rev": view["rev"],
    })
    assert response.status_code == 200, response.text


def prepare_wiki(root: Path) -> Path:
    """The real loader requires an index for the root and every subfolder."""
    wiki = root / "wiki" / "runbooks"
    wiki.mkdir(parents=True)
    (wiki.parent / "index.md").write_text(
        "---\ntitle: W7 acceptance wiki\nsummary: The scoped workspace lifecycle acceptance wiki.\n---\n"
        "[Runbooks](runbooks)\n"
    )
    (wiki / "index.md").write_text(
        "---\ntitle: W7 acceptance runbooks\nsummary: The workspace lifecycle acceptance runbook.\n---\n"
        "[Manage a workspace](runbooks/manage-a-workspace)\n"
    )
    shutil.copyfile(REPO / "docs/wiki/runbooks/manage-a-workspace.md", wiki / "manage-a-workspace.md")
    return wiki.parent


async def _start_server(root: Path, port: int, log):
    wiki = prepare_wiki(root)
    legacy = root / "legacy-empty"
    legacy.mkdir()
    config = root / "config.json"
    config.write_text(json.dumps(isolated_config(root, port)))
    # Child-only environment: no inherited database/auth/wiki overrides, no
    # changes to HOME or the parent process's configuration.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PRIMER_")}
    env.update(PRIMER_WIKI_DIR=str(wiki), PRIMER_AI_DOCS_DIR=str(legacy))
    return subprocess.Popen(
        [sys.executable, "-m", "primer", "api", "--config", str(config)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


@pytest.mark.asyncio
async def test_real_user_executes_workspace_runbook_through_tools(mock_llm, unique_suffix, tmp_path, unused_tcp_port):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    registry, mock_url = mock_llm
    target = f"w7-workspace-{unique_suffix}"
    model = f"w7-reader-{unique_suffix}"
    provider, agent_id = "w7-llm", "w7-reader"
    profile = profile_id_for(provider, model)
    payload = f"W7 café {unique_suffix}\n"
    expected_answer = "FILE-ANSWER:" + payload
    registry.register(model, [
        FileAnswerRule(),
        AfterWikiRule(emit_tool="system__call_tool", emit_args={
            "toolset_id": "workspaces", "tool_name": "read_workspace_file",
            "arguments": {"workspace_id": target, "path": "notes/welcome.txt", "encoding": "text"},
        }, emit_tool_call_id="w7_file_read"),
        Rule(when_tool_result=False, emit_tool="system__wiki_read", emit_args={"path": PAGE}, emit_tool_call_id="w7_wiki_read"),
    ])
    evidence = REPO / "tests/.e2e/w7-t1"
    evidence.mkdir(parents=True, exist_ok=True)
    trace = {"case": "test_real_user_executes_workspace_runbook_through_tools", "outcome": "running", "calls": []}
    process = None
    try:
        with (evidence / "server.stdout").open("wb") as log:
            process = await _start_server(tmp_path, unused_tcp_port, log)
            base = f"http://127.0.0.1:{unused_tcp_port}"
            async with httpx.AsyncClient(base_url=base, timeout=10) as admin:
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
                    pytest.fail("isolated W7 server did not become healthy")
                response = await admin.post("/v1/auth/register", json={"username": "w7-admin", "password": "w7-admin-password-123"})
                assert response.status_code in (200, 201), response.text
                response = await admin.post("/v1/auth/login", json={"username": "w7-admin", "password": "w7-admin-password-123"})
                assert response.status_code == 200, response.text
                setup = (
                    ("workspace_providers", {"id": "w7-local", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path / "workspaces")}}),
                    ("workspace_templates", {"id": "w7-template", "description": "W7 isolated template", "provider_id": "w7-local", "backend": {"kind": "local"}}),
                    ("llm_providers", {"id": provider, "provider": "openchat", "config": {"url": mock_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 1}}),
                    ("model_profiles", {"id": profile, "description": "W7 scripted reader", "provider_id": provider, "model_name": model, "context_length": 32000}),
                    ("agents", {"id": agent_id, "description": "W7 reader", "model": {"profile_id": profile}, "tools": ["system__wiki_read", "system__call_tool", "workspaces__read_workspace_file"], "system_prompt": ["Read the workspace runbook, then the requested file. Answer from the actual read result."]}),
                )
                for plural, body in setup:
                    response = await admin.post(f"/v1/{plural}", json=body)
                    assert response.status_code == 201, response.text
                    await _share(admin, plural, body["id"])
                response = await admin.post("/v1/setup/seed")
                assert response.status_code == 200, response.text
                response = await admin.post("/v1/admin/users", json={"username": "w7-user", "password": "w7-temporary-password-123", "role": "user"})
                assert response.status_code == 201, response.text
                uid = response.json()["id"]
                enabled = await admin.put("/v1/mcp_exposure", json={"enabled": True, "mode": "allowlist", "allowed_tools": sorted(ALLOWED)})
                assert enabled.status_code in (200, 204), enabled.text
                async with httpx.AsyncClient(base_url=base, timeout=10) as person:
                    response = await person.post("/v1/auth/login", json={"username": "w7-user", "password": "w7-temporary-password-123"})
                    assert response.status_code == 200, response.text
                    response = await person.post("/v1/auth/change-password", json={"current_password": "w7-temporary-password-123", "new_password": "w7-personal-password-123"})
                    assert response.status_code == 200, response.text
                    status = await person.get("/v1/auth/status")
                    assert status.status_code == 200, status.text
                    status = status.json()
                    assert status["role"] == "user" and status["principal"]["user_id"] == uid, status
                    default = status["default_workspace_id"]
                    assert default and default != target
                    before_default = await person.get(f"/v1/workspaces/{default}")
                    assert before_default.status_code == 200, before_default.text
                    assert before_default.json()["is_default"] is True
                    trace.update(user_id=uid, role="user", default_workspace=default, workspace=target,
                                 source_sha=os.environ.get("GITHUB_SHA", "local"),
                                 runbook_sha256=hashlib.sha256((REPO / "docs/wiki/runbooks/manage-a-workspace.md").read_bytes()).hexdigest())
                    cookie = "; ".join(f"{item.name}={item.value}" for item in person.cookies.jar)
                    async with mcp_http_client(cookie) as mcp_http:
                        async with streamable_http_client(f"{base}/v1/mcp/", http_client=mcp_http) as (read, write):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                assert {tool.name for tool in (await session.list_tools()).tools} == {"list_tools", "find_tools", "call_tool"}
                                discovery = await session.call_tool("list_tools", arguments={"limit": 100, "include_schema": True})
                                assert not discovery.is_error
                                catalogue = json.loads("".join(part.text for part in discovery.content if hasattr(part, "text")))
                                assert {entry["scoped_id"] for entry in catalogue["items"]} == ALLOWED, catalogue
                                trace["catalogue"] = catalogue

                                async def call(scoped, args):
                                    result = await session.call_tool("call_tool", arguments={"scoped_id": scoped, "arguments": args})
                                    envelope = json.loads("".join(part.text for part in result.content if hasattr(part, "text")))
                                    trace["calls"].append({"scoped_id": scoped, "arguments": args, "envelope": envelope})
                                    assert not result.is_error and envelope["ok"] is True, envelope
                                    return json.loads(envelope["output"])

                                wiki = await call("system__wiki_read", {"path": PAGE})
                                source_body = (REPO / "docs/wiki/runbooks/manage-a-workspace.md").read_text().split("---", 2)[2].strip()
                                assert wiki["path"] == PAGE and source_body in wiki["body"], wiki
                                templates = await call("workspaces__list_workspace_templates", {"limit": 200})
                                assert "w7-template" in {row["id"] for row in templates["items"]}
                                template = await call("workspaces__get_workspace_template", {"id": "w7-template"})
                                assert template["provider_id"] == "w7-local"
                                agent = await call("system__get_agent", {"id": agent_id})
                                choices = await call("system__list_model_profile_choices", {"limit": 200})
                                assert agent["model"]["profile_id"] == profile
                                assert profile in {row["id"] for row in choices["items"]}, choices
                                before = await call("workspaces__list_workspaces", {"limit": 200})
                                assert target not in {row["id"] for row in before["items"]}
                                for name in WRITES:
                                    gate = await call("system__describe_tool", {"toolset_id": "workspaces", "tool_name": name})
                                    assert gate["scoped_id"] == "workspaces__" + name and gate["approval"] == {"gated": False}, gate
                                created = await call("workspaces__create_workspace", {"id": target, "template_id": "w7-template"})
                                assert created["id"] == target and created["access"]["owners"] == [uid], created
                                stored = await person.get(f"/v1/workspaces/{target}")
                                assert stored.status_code == 200 and stored.json()["access"]["owners"] == [uid], stored.text
                                readback = await call("workspaces__get_workspace", {"id": target})
                                assert readback["template_snapshot"]["id"] == "w7-template" and readback["access"]["owners"] == [uid]
                                listing = await call("workspaces__list_workspace_files", {"workspace_id": target, "path": ".", "recursive": False, "limit": 20})
                                assert "items" in listing and not listing.get("truncated", False)
                                written = await call("workspaces__write_workspace_file", {"workspace_id": target, "path": "notes/welcome.txt", "content": payload, "encoding": "text"})
                                assert written["size_bytes"] == len(payload.encode())
                                file = await call("workspaces__read_workspace_file", {"workspace_id": target, "path": "notes/welcome.txt", "encoding": "text"})
                                assert file["content"] == payload and file["size_bytes"] == len(payload.encode())
                                assert (tmp_path / "workspaces" / target / "notes/welcome.txt").read_bytes() == payload.encode()
                                log_result = await call("workspaces__get_workspace_log", {"workspace_id": target, "limit": 10})
                                assert isinstance(log_result["commits"], list)  # state-repo history, not a file-write commit promise
                                started = await call("workspaces__create_workspace_session", {"workspace_id": target, "binding": {"kind": "agent", "agent_id": agent_id}, "initial_instructions": "Read notes/welcome.txt and report its contents. Do not write files.", "auto_start": True, "autonomous": True})
                                sid = started["id"]
                                deadline = asyncio.get_running_loop().time() + 60
                                while asyncio.get_running_loop().time() < deadline:
                                    observed = await call("workspaces__get_workspace_session", {"workspace_id": target, "session_id": sid})
                                    messages = await call("workspaces__read_workspace_session_messages", {"workspace_id": target, "session_id": sid, "limit": 200})
                                    if assistant_text(messages["items"]) == expected_answer and any(row["kind"] == "done" for row in messages["items"]):
                                        break
                                    await asyncio.sleep(0.2)
                                else:
                                    pytest.fail(f"real session never answered from its file read: {observed}")
                                assert observed["info"]["binding"]["agent_id"] == agent_id
                                assert not any(row["kind"] in {"error", "cancelled"} for row in messages["items"]), messages
                                durable = await person.get(f"/v1/sessions/{sid}")
                                assert durable.status_code == 200 and durable.json()["initiated_by"]["user_id"] == uid, durable.text
                                settled_by = asyncio.get_running_loop().time() + 10
                                while durable.json()["turn_status"] != "idle" and asyncio.get_running_loop().time() < settled_by:
                                    await asyncio.sleep(0.1)
                                    durable = await person.get(f"/v1/sessions/{sid}")
                                    assert durable.status_code == 200, durable.text
                                assert durable.json()["turn_status"] == "idle" and durable.json()["ended_reason"] == "completed", durable.text
                                actual_results = {}
                                for request in registry.requests:
                                    if request.get("model") == model:
                                        for message in request.get("messages", []):
                                            if message.get("role") == "tool":
                                                actual_results[message["tool_call_id"]] = json.loads(message["content"])
                                assert actual_results["w7_wiki_read"]["path"] == PAGE
                                assert source_body in actual_results["w7_wiki_read"]["body"]
                                assert actual_results["w7_file_read"]["content"] == payload
                                # The test's request explicitly agrees to destroying this disposable target only.
                                deleted = await call("workspaces__delete_workspace", {"id": target})
                                assert deleted == {"deleted": True, "id": target}
                                absent = await session.call_tool("call_tool", arguments={"scoped_id": "workspaces__get_workspace", "arguments": {"id": target}})
                                absent_envelope = json.loads("".join(part.text for part in absent.content if hasattr(part, "text")))
                                trace["post_delete_tool_read"] = absent_envelope
                                assert absent.is_error and absent_envelope["error"]["code"] == "tool_failed", absent_envelope
                                assert json.loads(absent_envelope["error"]["message"])["type"] == "not-found", absent_envelope
                                assert (await person.get(f"/v1/workspaces/{target}")).status_code == 404
                                assert not (tmp_path / "workspaces" / target).exists()
                                assert (await person.get(f"/v1/sessions/{sid}")).status_code == 404
                                after = await call("workspaces__list_workspaces", {"limit": 200})
                                assert after["total"] == len(after["items"]), after
                                assert target not in {row["id"] for row in after["items"]}
                                default_row = next(row for row in after["items"] if row["id"] == default)
                                assert default_row["is_default"] is True and default_row["access"] == before_default.json()["access"]
                                assert (tmp_path / "workspaces" / default).is_dir()
                                trace.update(outcome="passed", session_id=sid, file_content=payload, worker_tool_results=actual_results, session_records=messages["items"],
                                             session_answer=expected_answer, deleted=deleted, default_preserved=True)
    finally:
        if process is not None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        (evidence / "trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n")
