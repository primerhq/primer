"""CI-owned agent/graph authoring, real required approvals and execution.

Only the model HTTP responses are scripted. A private authenticated SQLite
instance runs the production MCP, worker, approval and graph paths. Import
and collection start no process; no shared exposure or policy is changed.
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
from tests.e2e.test_wiki_w7_t1_lifecycle import (
    _share,
    assistant_text,
    isolated_config,
    mcp_http_client,
    transcript_records,
)

REPO = Path(__file__).resolve().parents[2]
PAGES = ("runbooks/create-an-agent", "runbooks/create-and-run-a-graph")
GATED = ("create_agent", "update_agent", "create_graph", "update_graph", "delete_graph", "delete_agent")
OPERATIONS = (*GATED[:2], "invoke_agent", *GATED[2:4], "invoke_graph", *GATED[4:])
ALLOWED = {
    "system__wiki_read", "system__describe_tool", "system__list_model_profile_choices",
    "system__get_agent", "system__find_agents", "system__get_graph", "system__find_graphs",
    *["system__" + name for name in GATED],
    *["crud__" + name for name in ("create_agent", "update_agent", "create_graph", "update_graph")],
    "workspaces__create_workspace_session", "workspaces__get_workspace_session",
    "workspaces__read_workspace_session_messages", "workspaces__get_workspace",
}


class PromptAnswerRule(Rule):
    """The subject agent answers from the actual runtime user message."""

    def matches(self, request: dict) -> bool:
        user = next((m for m in reversed(request.get("messages", [])) if m.get("role") == "user"), {})
        text = user.get("content")
        assert isinstance(text, str) and text, user
        self.emit_text = "AGENT-ANSWER:" + text
        return True


class ActionRule(Rule):
    """Issue one operation; derive the final answer only from its real result."""

    def __init__(self, label: str, scoped: str, arguments: dict):
        super().__init__(when_last_user_contains=label)
        self.label, self.scoped, self.arguments = label, scoped, arguments

    def matches(self, request: dict) -> bool:
        if not super().matches(request):
            return False
        results = [m for m in request.get("messages", []) if m.get("role") == "tool" and m.get("tool_call_id") == self.label]
        if results:
            result = json.loads(results[-1]["content"])
            assert isinstance(result, dict) and "error" not in result, result
            assert not ("type" in result and "message" in result), result
            self.emit_tool, self.emit_args = None, {}
            self.emit_text = "RESULT:" + json.dumps(result, ensure_ascii=False, sort_keys=True)
        else:
            toolset, name = self.scoped.split("__")
            self.emit_text, self.emit_tool = None, "system__call_tool"
            self.emit_args = {"toolset_id": toolset, "tool_name": name, "arguments": self.arguments}
            self.emit_tool_call_id = self.label
        return True


def prepare_wiki(root: Path) -> Path:
    wiki = root / "wiki"
    (wiki / "runbooks").mkdir(parents=True)
    (wiki / "index.md").write_text("---\ntitle: W7 T2 acceptance\nsummary: Scoped agent and graph runbooks.\n---\n[Runbooks](runbooks)\n")
    (wiki / "runbooks/index.md").write_text(
        "---\ntitle: W7 T2 runbooks\nsummary: The two authoring flows.\n---\n"
        + "\n".join(f"[{path}]({path})" for path in PAGES) + "\n"
    )
    for path in PAGES:
        shutil.copyfile(REPO / "docs/wiki" / (path + ".md"), wiki / (path + ".md"))
    return wiki


def start_server(root: Path, port: int, log):
    wiki = prepare_wiki(root)
    legacy = root / "legacy-empty"
    legacy.mkdir()
    config = root / "config.json"
    config.write_text(json.dumps(isolated_config(root, port)))
    env = {key: value for key, value in os.environ.items() if not key.startswith("PRIMER_")}
    env.update(PRIMER_WIKI_DIR=str(wiki), PRIMER_AI_DOCS_DIR=str(legacy))
    return subprocess.Popen(
        [sys.executable, "-m", "primer", "api", "--config", str(config)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def check_pending(pending: dict, operation: str, arguments: dict, policy_id: str) -> None:
    # The meta-tool parks its INNER bare name. Bind it to the exact policy
    # describe_tool served for system/name, arguments and gate occurrence.
    assert pending["tool_name"] == operation and pending["policy_id"] == policy_id, pending
    assert pending["approval_type"] == "required" and pending["gate_id"], pending
    assert pending["arguments"] == arguments, pending


def stage_answer(records: list[dict]) -> tuple[str, dict]:
    """Read the parent reply while retaining failures from every child run."""
    from primer.session.terminals import closes_turn, is_delegated

    assert not any(row["kind"] in {"error", "cancelled"} for row in records), records
    parent = [row for row in records if not is_delegated(row) and row.get("node_id") is None]
    assert any(row["kind"] == "done" and closes_turn(row) for row in parent), records
    answer = assistant_text(parent)
    assert answer.startswith("RESULT:"), answer
    return answer, json.loads(answer.removeprefix("RESULT:"))


def require_evidence(trace: dict, source_sha: str, runbook_hashes: dict) -> None:
    """The CI step fails on absence, collection, precondition stop or a partial run."""
    assert trace["case"] == "test_user_authors_and_runs_agent_and_graph_with_real_approvals"
    assert trace["outcome"] == "passed" and trace["source_sha"] == source_sha
    assert trace["runbook_hashes"] == runbook_hashes
    assert trace["role"] == "user" and trace["user_id"]
    assert trace["approver"]["role"] == "admin" and trace["approver"]["user_id"] != trace["user_id"]
    assert set(trace["stages"]) == set(OPERATIONS)
    assert [a["operation"] for a in trace["approvals"]] == list(GATED)
    assert len({a["gate_id"] for a in trace["approvals"]}) == len(GATED)
    for approval in trace["approvals"]:
        assert approval["type"] == "required" and approval["gate_id"] and approval["policy_id"]
        assert approval["session_id"] == trace["stages"][approval["operation"]]["session_id"]
        assert approval["effect_absent_before_approval"] is True
        assert approval["decision_http"] == 202
    for stage in trace["stages"].values():
        assert stage["initiator"] == trace["user_id"] and stage["completed"] is True
        assert stage["answer"].startswith("RESULT:")
        assert stage["result"] == json.loads(stage["answer"].removeprefix("RESULT:"))
    assert trace["graph_session"]["completed"] is True
    assert trace["graph_session"]["end_output"] == trace["stages"]["invoke_graph"]["result"]["output"]
    assert trace["graph_session"]["end_output"] == "GRAPH-ANSWER:AGENT-ANSWER:" + trace["input"]
    assert trace["stages"]["invoke_agent"]["result"]["output"] == "AGENT-ANSWER:" + trace["input"]
    assert trace["deleted_agent"] and trace["deleted_graph"] and trace["default_preserved"]


def action_arguments(agent_id: str, graph_id: str, subject_profile: str, text: str) -> dict:
    """The exact sampled call bodies, validated by the safe schema tests."""
    agent = {"id": agent_id, "description": "T2 disposable subject", "model": {"profile_id": subject_profile},
             "tools": ["system__wiki_read"], "system_prompt": ["Answer the supplied message."], "max_tool_turns": 8, "max_output_tokens": 1024}
    updated_agent = {**agent, "description": "T2 subject updated after approval", "max_output_tokens": 1536}
    graph = {"id": graph_id, "description": "T2 disposable graph", "max_iterations": 8, "nodes": [
        {"kind": "begin", "id": "begin", "input_schema": {"type": "string", "minLength": 1}},
        {"kind": "agent", "id": "answer", "agent_id": agent_id, "input_template": "{{ nodes.begin.text }}"},
        {"kind": "end", "id": "end", "output_template": "GRAPH-ANSWER:{{ nodes.answer.text }}"},
    ], "edges": [{"kind": "static", "from_node": "begin", "to_node": "answer"}, {"kind": "static", "from_node": "answer", "to_node": "end"}]}
    updated_graph = {**graph, "description": "T2 graph updated after approval", "max_iterations": 10}
    return {
        "create_agent": {"entity": agent}, "update_agent": {"id": agent_id, "entity": updated_agent},
        "invoke_agent": {"agent_id": agent_id, "prompt": text},
        "create_graph": {"entity": graph}, "update_graph": {"id": graph_id, "entity": updated_graph},
        "invoke_graph": {"graph_id": graph_id, "input": text},
        "delete_graph": {"id": graph_id}, "delete_agent": {"id": agent_id},
    }


@pytest.mark.asyncio
async def test_user_authors_and_runs_agent_and_graph_with_real_approvals(mock_llm, unique_suffix, tmp_path, unused_tcp_port):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    registry, mock_url = mock_llm
    provider, driver = "w7-t2-llm", "w7-t2-author"
    builder_model, subject_model = (f"w7-t2-{kind}-{unique_suffix}" for kind in ("builder", "subject"))
    builder_profile, subject_profile = (profile_id_for(provider, model) for model in (builder_model, subject_model))
    agent_id, graph_id = f"w7-agent-{unique_suffix}", f"w7-graph-{unique_suffix}"
    text = f"W7 café {unique_suffix}"
    actions = action_arguments(agent_id, graph_id, subject_profile, text)
    registry.register(subject_model, [PromptAnswerRule()])
    registry.register(builder_model, [ActionRule(name, ("workspace_ext" if name == "invoke_graph" else "system") + "__" + name, actions[name]) for name in OPERATIONS])
    evidence = REPO / "tests/.e2e/w7-t2"
    evidence.mkdir(parents=True, exist_ok=True)
    hashes = {path: hashlib.sha256((REPO / "docs/wiki" / (path + ".md")).read_bytes()).hexdigest() for path in PAGES}
    trace = {"case": "test_user_authors_and_runs_agent_and_graph_with_real_approvals", "outcome": "running", "calls": [], "approvals": [], "stages": {},
             "source_sha": os.environ.get("GITHUB_SHA", "local"), "runbook_hashes": hashes, "input": text}
    process = None
    try:
        with (evidence / "server.stdout").open("wb") as log:
            process = start_server(tmp_path, unused_tcp_port, log)
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
                    pytest.fail("isolated T2 server did not become healthy")
                for route in ("register", "login"):
                    response = await admin.post("/v1/auth/" + route, json={"username": "w7-t2-admin", "password": "w7-admin-password-123"})
                    assert response.status_code in (200, 201), response.text
                admin_status = await admin.get("/v1/auth/status")
                assert admin_status.status_code == 200 and admin_status.json()["role"] == "admin", admin_status.text
                trace["approver"] = {"role": "admin", "user_id": admin_status.json()["principal"]["user_id"]}
                setup = [
                    ("workspace_providers", {"id": "w7-local", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path / "workspaces")}}),
                    ("workspace_templates", {"id": "w7-template", "description": "T2 isolated template", "provider_id": "w7-local", "backend": {"kind": "local"}}),
                    ("llm_providers", {"id": provider, "provider": "openchat", "config": {"url": mock_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 2}}),
                    *[("model_profiles", {"id": profile, "description": "T2 scripted model", "provider_id": provider, "model_name": model, "context_length": 32000}) for profile, model in ((builder_profile, builder_model), (subject_profile, subject_model))],
                    ("agents", {"id": driver, "description": "T2 approval driver", "model": {"profile_id": builder_profile}, "tools": ["system__call_tool", "workspace_ext__invoke_graph"], "system_prompt": ["Execute the requested single operation once; await approval when gated."], "max_tool_turns": 8}),
                ]
                for plural, body in setup:
                    response = await admin.post("/v1/" + plural, json=body)
                    assert response.status_code == 201, response.text
                    await _share(admin, plural, body["id"])
                response = await admin.post("/v1/setup/seed")
                assert response.status_code == 200, response.text
                response = await admin.post("/v1/admin/users", json={"username": "w7-t2-user", "password": "w7-temporary-password-123", "role": "user"})
                assert response.status_code == 201, response.text
                uid = response.json()["id"]
                response = await admin.put("/v1/mcp_exposure", json={"enabled": True, "mode": "allowlist", "allowed_tools": sorted(ALLOWED)})
                assert response.status_code in (200, 204), response.text
                async with httpx.AsyncClient(base_url=base, timeout=10) as person:
                    response = await person.post("/v1/auth/login", json={"username": "w7-t2-user", "password": "w7-temporary-password-123"})
                    assert response.status_code == 200, response.text
                    response = await person.post("/v1/auth/change-password", json={"current_password": "w7-temporary-password-123", "new_password": "w7-personal-password-123"})
                    assert response.status_code == 200, response.text
                    status = await person.get("/v1/auth/status")
                    assert status.status_code == 200, status.text
                    status = status.json()
                    assert status["role"] == "user" and status["principal"]["user_id"] == uid
                    workspace = status["default_workspace_id"]
                    before = await person.get(f"/v1/workspaces/{workspace}")
                    assert before.status_code == 200 and before.json()["is_default"] is True
                    trace.update(role="user", user_id=uid, workspace=workspace)
                    flow_deadline = asyncio.get_running_loop().time() + 100
                    cookie = "; ".join(f"{item.name}={item.value}" for item in person.cookies.jar)
                    async with mcp_http_client(cookie) as mcp_http:
                        async with streamable_http_client(f"{base}/v1/mcp/", http_client=mcp_http) as (read, write):
                            async with ClientSession(read, write) as session:
                                await session.initialize()
                                assert {tool.name for tool in (await session.list_tools()).tools} == {"list_tools", "find_tools", "call_tool"}

                                async def call(scoped, args):
                                    result = await session.call_tool("call_tool", arguments={"scoped_id": scoped, "arguments": args})
                                    envelope = json.loads("".join(p.text for p in result.content if hasattr(p, "text")))
                                    trace["calls"].append({"scoped_id": scoped, "arguments": args, "envelope": envelope})
                                    assert not result.is_error and envelope["ok"] is True, envelope
                                    return json.loads(envelope["output"])

                                for path in PAGES:
                                    page = await call("system__wiki_read", {"path": path})
                                    source = (REPO / "docs/wiki" / (path + ".md")).read_text().split("---", 2)[2].strip()
                                    assert page["path"] == path and source in page["body"]
                                choices = await call("system__list_model_profile_choices", {"limit": 200})
                                assert subject_profile in {row["id"] for row in choices["items"]}
                                policies = {}
                                for toolset, names in (("system", GATED), ("crud", GATED[:4])):
                                    for name in names:
                                        gate = await call("system__describe_tool", {"toolset_id": toolset, "tool_name": name})
                                        assert gate["required_role"] == "user" and gate["approval"]["gated"] and gate["approval"]["type"] == "required", gate
                                        if toolset == "system":
                                            assert gate["approval"]["policy_id"], gate
                                            policies[name] = gate["approval"]["policy_id"]

                                async def completed(sid):
                                    while asyncio.get_running_loop().time() < flow_deadline:
                                        row = await person.get(f"/v1/sessions/{sid}")
                                        assert row.status_code == 200, row.text
                                        value = row.json()
                                        assert value["initiated_by"]["user_id"] == uid
                                        if value["turn_status"] == "idle" and value["ended_reason"] == "completed":
                                            return value
                                        assert value["ended_reason"] not in {"failed", "cancelled"}, value
                                        await asyncio.sleep(0.1)
                                    pytest.fail(f"T2 session {sid} never completed")

                                async def stage(name):
                                    started = await call("workspaces__create_workspace_session", {"workspace_id": workspace, "binding": {"kind": "agent", "agent_id": driver}, "initial_instructions": name, "auto_start": True, "autonomous": True})
                                    sid = started["id"]
                                    if name in GATED:
                                        while asyncio.get_running_loop().time() < flow_deadline:
                                            response = await admin.get(f"/v1/sessions/{sid}/tool_approval/pending")
                                            if response.status_code == 200:
                                                pending = response.json()
                                                break
                                            assert response.status_code == 404, response.text
                                            await asyncio.sleep(0.1)
                                        else:
                                            pytest.fail(f"{name} never parked for its required approval")
                                        check_pending(pending, name, actions[name], policies[name])
                                        plural, target = ("graphs", graph_id) if "graph" in name else ("agents", agent_id)
                                        prior = await person.get(f"/v1/{plural}/{target}")
                                        if name.startswith("create_"):
                                            assert prior.status_code == 404, prior.text
                                        else:
                                            assert prior.status_code == 200 and prior.json()["access"]["owners"] == [uid], prior.text
                                            if name.startswith("update_"):
                                                assert prior.json()["description"] != actions[name]["entity"]["description"]
                                        decision = await admin.post(f"/v1/sessions/{sid}/tool_approval/respond", json={"tool_call_id": pending["tool_call_id"], "gate_id": pending["gate_id"], "decision": "approved", "reason": "Approve this disposable T2 fixture operation"})
                                        assert decision.status_code == 202 and decision.json() == {"status": "accepted"}, decision.text
                                        trace["approvals"].append({"operation": name, "session_id": sid, "policy_id": pending["policy_id"], "type": pending["approval_type"], "gate_id": pending["gate_id"], "pending": pending,
                                                                   "before_approval": {"http": prior.status_code, "row": prior.json()}, "effect_absent_before_approval": True, "decision_http": decision.status_code})
                                    await completed(sid)
                                    messages = await call("workspaces__read_workspace_session_messages", {"workspace_id": workspace, "session_id": sid, "limit": 200})
                                    records = transcript_records(messages["items"])
                                    answer, result = stage_answer(records)
                                    # Both the mock's next request and durable tokens must carry the actual tool result.
                                    results = [json.loads(m["content"]) for req in registry.requests if req.get("model") == builder_model for m in req.get("messages", []) if m.get("role") == "tool" and m.get("tool_call_id") == name]
                                    assert results and results[-1] == result
                                    trace["stages"][name] = {"session_id": sid, "initiator": uid, "completed": True, "answer": answer, "result": result, "records": records}
                                    return result

                                for name in OPERATIONS:
                                    result = await stage(name)
                                    if name in ("create_agent", "update_agent", "create_graph", "update_graph"):
                                        kind = "graph" if "graph" in name else "agent"
                                        target = graph_id if kind == "graph" else agent_id
                                        durable = await call("system__get_" + kind, {"id": target})
                                        assert durable == result and durable["access"]["owners"] == [uid]
                                        from primer.model.agent import Agent
                                        from primer.model.graph import Graph

                                        entity = actions[name]["entity"]
                                        expected = (Graph if kind == "graph" else Agent).model_validate(entity).model_dump(mode="json")
                                        assert all(durable[key] == expected[key] for key in entity), durable
                                    if name == "invoke_agent":
                                        assert result == {"output": "AGENT-ANSWER:" + text}, result
                                    if name == "invoke_graph":
                                        assert result == {"output": "GRAPH-ANSWER:AGENT-ANSWER:" + text}, result
                                        bound = await call("workspaces__create_workspace_session", {"workspace_id": workspace, "binding": {"kind": "graph", "graph_id": graph_id}, "graph_input": text, "auto_start": True, "autonomous": True})
                                        await completed(bound["id"])
                                        transcript = await call("workspaces__read_workspace_session_messages", {"workspace_id": workspace, "session_id": bound["id"], "limit": 200})
                                        records = transcript_records(transcript["items"])
                                        end = [r for r in records if r["kind"] == "assistant_token" and r["payload"].get("end_node_id") == "end"]
                                        assert end and end[-1]["payload"]["text"] == result["output"], records
                                        assert any(r["kind"] == "done" and r["payload"].get("graph_end") for r in records)
                                        assert not any(r["kind"] in {"error", "cancelled"} for r in records)
                                        trace["graph_session"] = {"id": bound["id"], "completed": True, "end_output": end[-1]["payload"]["text"], "records": records}
                                    if name.startswith("delete_"):
                                        kind = "graph" if name == "delete_graph" else "agent"
                                        target = graph_id if kind == "graph" else agent_id
                                        assert result == {"deleted": True, "id": target}, result
                                        absent = await person.get(f"/v1/{kind}s/{target}")
                                        assert absent.status_code == 404, absent.text
                                        found = await call("system__find_" + kind + "s", {"predicate": {"kind": "predicate", "left": {"kind": "field", "name": "id"}, "op": "=", "right": {"kind": "value", "value": target}}})
                                        assert found["items"] == [], found
                                        trace["deleted_" + kind] = True
                                after = await call("workspaces__get_workspace", {"id": workspace})
                                assert after["is_default"] and after["access"] == before.json()["access"]
                                assert (tmp_path / "workspaces" / workspace).is_dir()
                                trace.update(default_preserved=True, outcome="passed")
                                require_evidence(trace, trace["source_sha"], hashes)
    finally:
        if process is not None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        (evidence / "trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n")
