"""CI-owned real channel onboarding with private Slack/LLM wire fixtures.

Production auth, MCP, approval, worker, Slack SDK/adapter, normalization, routing
and reply relay run in a private child. External Slack Socket Mode handshake is
replaced at the raw-event boundary; no shared provider/configuration is touched.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

from primer.model.workspace_session import SessionInfo
from tests._support.mock_llm import Rule
from tests._support.mock_slack_wire import SlackWire, slack_server
from tests._support.model_profiles import profile_id_for
from tests.e2e.test_wiki_w7_t1_lifecycle import _share, isolated_config, mcp_http_client, transcript_records
from tests.e2e.test_wiki_w7_t2_lifecycle import stage_answer

REPO = Path(__file__).resolve().parents[2]
PAGE = "runbooks/onboard-a-channel"
CASE = "test_user_onboards_channel_and_receives_real_worker_replies"
SPECS = {
    "reject_create": ("create", "owner", "rejected"),
    "denied_create": ("create", "stranger", "approved"),
    "create": ("create", "owner", "approved"),
    "delete": ("delete", "owner", "approved"),
}
ALLOWED = {
    "system__wiki_read", "system__describe_tool", "system__create_channel_provider",
    "system__get_channel_provider", "system__list_channel_providers",
    "system__create_channel", "system__get_channel", "system__create_channel_binding",
    "system__list_channel_bindings", "system__delete_channel_binding",
    "system__set_reply_binding", "system__clear_reply_binding",
    "trigger__create", "trigger__get", "trigger__list", "trigger__fire_now", "trigger__delete",
    "workspaces__create_workspace", "workspaces__get_workspace", "workspaces__list_workspace_sessions",
    "workspaces__get_workspace_session",
}


def gate_result(name: str, value) -> None:
    if name == "reject_create":
        assert value["rejected"] is True and value["tool_name"] == "create"
    elif name == "denied_create":
        assert value["type"] == "not-found"
    elif name == "create":
        assert value["id"] and value["config"]["kind"] == "channel"
        assert value["owner"]["type"] == "user" and value["owner"]["role"] == "user"
    elif name == "delete":
        assert value == {"ok": True}
    else:
        raise AssertionError("unknown channel gate")


class GateRule(Rule):
    def __init__(self, name: str, arguments: dict):
        super().__init__()
        self.name, self.arguments = name, arguments

    def matches(self, request: dict) -> bool:
        if not any(m.get("role") == "user" and m.get("content") == self.name for m in request.get("messages", [])):
            return False
        last = next((m for m in reversed(request["messages"]) if m.get("role") == "tool"), {})
        if last.get("tool_call_id") == self.name:
            value = json.loads(last["content"])
            gate_result(self.name, value)
            self.emit_tool, self.emit_text = None, "RESULT:" + json.dumps(value)
        else:
            self.emit_text = None
            self.emit_tool, self.emit_tool_call_id = "system__call_tool", self.name
            self.emit_args = {"toolset_id": "trigger", "tool_name": SPECS[self.name][0], "arguments": self.arguments}
        return True


class ReplyRule(Rule):
    def matches(self, request: dict) -> bool:
        last = next((m.get("content") for m in reversed(request.get("messages", [])) if m.get("role") == "user"), None)
        if not isinstance(last, str) or not last.startswith(("help:", "manual:")):
            return False
        self.emit_text = "RESULT:" + json.dumps({"received": last, "answer": "CHANNEL:" + last})
        return True


def read_gate(items: list[dict], name: str):
    from primer.model.chat import Message, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord

    expected_error = name in {"reject_create", "denied_create"}
    records, errors = [], []
    for row in items:
        if "kind" in row:
            SessionMessageRecord.model_validate(row)
            records.append(row)
        else:
            for part in Message.model_validate(row).parts:
                if isinstance(part, ToolResultPart) and part.error:
                    assert expected_error and part.id == name
                    gate_result(name, json.loads(part.output))
                    errors.append(part)
    assert len(errors) == int(expected_error)
    event_errors = [r for r in records if r["kind"] == "tool_result" and r["payload"].get("error")]
    assert len(event_errors) == int(expected_error)
    if event_errors:
        gate_result(name, json.loads(event_errors[0]["payload"]["output"]))
    answer, result = stage_answer(records)
    gate_result(name, result)
    raw = [p for row in items if "kind" not in row for p in Message.model_validate(row).parts if isinstance(p, ToolResultPart)]
    assert any(p.id == name and p.error is expected_error and json.loads(p.output) == result for p in raw)
    return records, answer, result


def read_session_info(value: dict, workspace_id: str, agent_id: str) -> SessionInfo:
    """Workspace tools serve SessionInfo, whose identifier is session_id."""
    info = SessionInfo.model_validate(value)
    assert info.workspace_id == workspace_id and info.agent_id == agent_id
    return info


def start_server(root: Path, port: int, slack_url: str, log):
    wiki = root / "wiki"
    (wiki / "runbooks").mkdir(parents=True)
    (wiki / "index.md").write_text("---\ntitle: T4 acceptance\nsummary: Channel onboarding.\n---\n[Runbooks](runbooks)\n")
    (wiki / "runbooks/index.md").write_text("---\ntitle: T4 runbook\nsummary: The channel flow.\n---\n[Onboard a channel](" + PAGE + ")\n")
    shutil.copyfile(REPO / "docs/wiki" / (PAGE + ".md"), wiki / (PAGE + ".md"))
    (root / "legacy-empty").mkdir()
    config = root / "config.json"
    config.write_text(json.dumps(isolated_config(root, port)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRIMER_")}
    env.update(PRIMER_WIKI_DIR=str(wiki), PRIMER_AI_DOCS_DIR=str(root / "legacy-empty"))
    return subprocess.Popen([
        sys.executable, "-m", "tests._support.w7_channel_server", "--config", str(config), "--slack-url", slack_url,
    ], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)


@pytest.mark.asyncio
async def test_user_onboards_channel_and_receives_real_worker_replies(mock_llm, unique_suffix, tmp_path, unused_tcp_port):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    registry, llm_url = mock_llm
    model, llm, driver, responder = "w7-t4-" + unique_suffix, "w7-llm", "w7-driver", "w7-responder"
    profile = profile_id_for(llm, model)
    provider, channel, workspace = ("w7-" + name + "-" + unique_suffix for name in ("slack", "room", "work"))
    external = "C" + unique_suffix.upper()
    source_config = {"kind": "channel", "provider_id": provider, "channel_id": channel, "interactive": True}
    actions = {name: {"slug": "w7-" + name.replace("_", "-") + "-" + unique_suffix, "name": name, "config": source_config}
               for name in ("reject_create", "denied_create", "create")}
    actions["delete"] = {}  # populated with the actual returned trigger id
    registry.register(model, [*[GateRule(name, actions[name]) for name in SPECS], ReplyRule()])
    wire = SlackWire()
    evidence = REPO / "tests/.e2e/w7-t4"
    evidence.mkdir(parents=True, exist_ok=True)
    page_hash = hashlib.sha256((REPO / "docs/wiki" / (PAGE + ".md")).read_bytes()).hexdigest()
    trace = {"case": CASE, "source_sha": os.environ.get("GITHUB_SHA", "local"), "runbook_hash": page_hash,
             "outcome": "running", "identities": {}, "calls": [], "gates": [], "stages": {}, "checks": {},
             "channel": channel, "provider": provider, "workspace": workspace, "external": external, "actions": actions}
    process = None
    with slack_server(wire) as slack_url:
        try:
            with (evidence / "server.stdout").open("wb") as log:
                process = start_server(tmp_path, unused_tcp_port, slack_url, log)
                base = f"http://127.0.0.1:{unused_tcp_port}"
                async with AsyncExitStack() as stack:
                    admin = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=10))
                    ready = asyncio.get_running_loop().time() + 45
                    while asyncio.get_running_loop().time() < ready:
                        assert process.poll() is None, (evidence / "server.stdout").read_text()[-6000:]
                        try:
                            if (await admin.get("/v1/health")).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(.2)
                    else:
                        pytest.fail("private channel child never became healthy")
                    for verb in ("register", "login"):
                        response = await admin.post("/v1/auth/" + verb, json={"username": "w7-admin", "password": "private-w7-password-123"})
                        assert response.status_code in (200, 201), response.text
                    status = (await admin.get("/v1/auth/status")).json()
                    assert status["role"] == "admin"
                    trace["approver"] = status["principal"]["user_id"]
                    setup = [
                        ("workspace_providers", {"id": "w7-local", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path / "workspaces")}}),
                        ("workspace_templates", {"id": "w7-template", "description": "Private T4 template", "provider_id": "w7-local", "backend": {"kind": "local"}}),
                        ("llm_providers", {"id": llm, "provider": "openchat", "config": {"url": llm_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 2}}),
                        ("model_profiles", {"id": profile, "description": "T4 scripted model", "provider_id": llm, "model_name": model, "context_length": 32000}),
                    ]
                    for aid, granted in ((driver, ["system__call_tool"]), (responder, [])):
                        setup.append(("agents", {"id": aid, "description": "T4 private " + aid, "model": {"profile_id": profile},
                                                "tools": granted, "system_prompt": ["Perform the requested private operation."], "max_tool_turns": 8}))
                    for plural, body in setup:
                        response = await admin.post("/v1/" + plural, json=body)
                        assert response.status_code == 201, response.text
                        await _share(admin, plural, body["id"])
                    assert (await admin.post("/v1/setup/seed")).status_code == 200
                    clients, defaults = {"admin": admin}, {}
                    for role in ("owner", "reader", "stranger"):
                        username, password = "w7-" + role, "private-w7-user-password-123"
                        response = await admin.post("/v1/admin/users", json={"username": username, "password": password, "role": "user"})
                        assert response.status_code == 201, response.text
                        client = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=10))
                        assert (await client.post("/v1/auth/login", json={"username": username, "password": password})).status_code == 200
                        status = (await client.get("/v1/auth/status")).json()
                        assert status["role"] == "user" and status["principal"]["user_id"] == response.json()["id"]
                        trace["identities"][role] = response.json()["id"]
                        clients[role], defaults[role] = client, status["default_workspace_id"]
                    owner = clients["owner"]
                    default_before = (await owner.get("/v1/workspaces/" + defaults["owner"])).json()
                    assert default_before["is_default"]
                    response = await admin.put("/v1/mcp_exposure", json={"enabled": True, "mode": "allowlist", "allowed_tools": sorted(ALLOWED)})
                    assert response.status_code in (200, 204), response.text
                    sessions = {}
                    for role, client in clients.items():
                        cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
                        http = mcp_http_client(cookie)
                        await stack.enter_async_context(http)
                        read, write = await stack.enter_async_context(streamable_http_client(base + "/v1/mcp/", http_client=http))
                        sdk = await stack.enter_async_context(ClientSession(read, write))
                        await sdk.initialize()
                        assert {t.name for t in (await sdk.list_tools()).tools} == {"list_tools", "find_tools", "call_tool"}
                        sessions[role] = sdk

                    async def call(scoped, args, *, role="owner", error=None):
                        result = await sessions[role].call_tool("call_tool", arguments={"scoped_id": scoped, "arguments": args})
                        envelope = json.loads("".join(p.text for p in result.content if hasattr(p, "text")))
                        trace["calls"].append({"actor": role, "scoped_id": scoped, "arguments": args, "envelope": envelope})
                        if error is not None:
                            assert envelope["ok"] is False and envelope["error"]["code"] == error, envelope
                            return envelope
                        assert not result.is_error and envelope["ok"] is True, envelope
                        return json.loads(envelope["output"])

                    page = await call("system__wiki_read", {"path": PAGE})
                    assert (REPO / "docs/wiki" / (PAGE + ".md")).read_text().split("---", 2)[2].strip() in page["body"]
                    await call("system__list_channel_providers", {"limit": 200}, error="tool_unavailable")
                    registered = await call("system__create_channel_provider", {"entity": {
                        "id": provider, "provider": "slack", "config": {"app_token": "xapp-w7-private", "bot_token": "xoxb-w7-private"},
                    }}, role="admin")
                    reread = await call("system__get_channel_provider", {"id": provider}, role="admin")
                    assert registered["id"] == reread["id"] == provider
                    assert all(set(reread["config"][field]) == {"*"} for field in ("app_token", "bot_token"))
                    trace["provider_readback"] = reread
                    await _share(admin, "channel_providers", provider)
                    target = await call("workspaces__create_workspace", {"id": workspace, "template_id": "w7-template"})
                    assert target["access"]["owners"] == [trace["identities"]["owner"]]
                    room_body = {"id": channel, "provider_id": provider, "provider": "slack", "external_id": external,
                                 "label": "Private T4 room", "config": {"chats": {"enabled": True}}}
                    room = await call("system__create_channel", {"entity": room_body})
                    assert room["access"]["owners"] == [trace["identities"]["owner"]]
                    trace["room"] = room
                    policies = {}
                    for name in ("create", "delete"):
                        detail = await call("system__describe_tool", {"toolset_id": "trigger", "tool_name": name})
                        assert detail["required_role"] == "user" and detail["approval"]["type"] == "required" and detail["approval"]["gated"]
                        policies[name] = detail["approval"]["policy_id"]
                    await call("trigger__create", actions["create"], error="tool_unavailable")
                    deadline = asyncio.get_running_loop().time() + 100

                    async def trigger_rows():
                        return await call("trigger__list", {"kind": "channel"})

                    async def stage(name):
                        operation, role, decision = SPECS[name]
                        before = await trigger_rows()
                        response = await clients[role].post("/v1/workspaces/" + defaults[role] + "/sessions", json={
                            "binding": {"kind": "agent", "agent_id": driver}, "initial_instructions": name, "auto_start": True, "autonomous": True,
                        })
                        assert response.status_code == 201, response.text
                        started = response.json();sid = started["id"]
                        assert started["initiated_by"]["user_id"] == trace["identities"][role] and started["initiated_by"]["role"] == "user"
                        while asyncio.get_running_loop().time() < deadline:
                            response = await admin.get(f"/v1/sessions/{sid}/tool_approval/pending")
                            if response.status_code == 200:
                                pending = response.json();break
                            assert response.status_code == 404, response.text
                            await asyncio.sleep(.1)
                        else:
                            pytest.fail("channel gate never parked: " + name)
                        assert pending["tool_name"] == operation and pending["approval_type"] == "required"
                        assert pending["policy_id"] == policies[operation] and pending["arguments"] == actions[name] and pending["gate_id"]
                        assert await trigger_rows() == before
                        response = await admin.post(f"/v1/sessions/{sid}/tool_approval/respond", json={
                            "tool_call_id": pending["tool_call_id"], "gate_id": pending["gate_id"], "decision": decision, "reason": "Private T4 " + name,
                        })
                        assert response.status_code == 202 and response.json() == {"status": "accepted"}, response.text
                        while asyncio.get_running_loop().time() < deadline:
                            current = await clients[role].get(f"/v1/sessions/{sid}")
                            assert current.status_code == 200
                            row = current.json()
                            assert row["initiated_by"]["user_id"] == trace["identities"][role] and row["ended_reason"] not in {"failed", "cancelled"}
                            if row["turn_status"] == "idle" and row["ended_reason"] == "completed":
                                break
                            await asyncio.sleep(.1)
                        else:
                            pytest.fail("channel gate never completed: " + name)
                        items = (await admin.get(f"/v1/sessions/{sid}/messages", params={"limit": 200})).json()["items"]
                        records, answer, value = read_gate(items, name)
                        after = await trigger_rows()
                        if name in {"reject_create", "denied_create"}:
                            assert before == after
                        trace["gates"].append({"stage": name, "operation": operation, "decision": decision, "http": 202,
                                               "policy_id": pending["policy_id"], "gate_id": pending["gate_id"], "pending": pending})
                        trace["stages"][name] = {"actor": role, "initiator": trace["identities"][role], "started": started, "session_id": sid,
                                                 "before": before, "after": after, "items": items, "records": records, "answer": answer, "result": value}
                        return value

                    await stage("reject_create")
                    await stage("denied_create")
                    created = await stage("create");trigger = created["id"]
                    actions["delete"]["id"] = trigger
                    assert created["access"]["owners"] == [trace["identities"]["owner"]]
                    assert created["owner"]["user_id"] == trace["identities"]["owner"]
                    trace["trigger"] = trigger
                    binding_args = {"trigger_id": trigger, "event_matcher": {"event_type": "message.posted", "text_pattern": "^help:"},
                                    "config": {"kind": "agent_fresh_session", "workspace_id": workspace, "agent_id": responder},
                                    "payload_template": "{{ event.text }}", "reply_target": "source_thread"}
                    binding = await call("system__create_channel_binding", binding_args)
                    assert binding["owner"]["user_id"] == trace["identities"]["owner"] and binding["owner"]["role"] == "user"
                    assert binding["owner"]["type"] == "user" and binding["config"] == binding_args["config"]
                    trace["binding"] = binding;trace["binding_arguments"] = binding_args
                    listed = await call("system__list_channel_bindings", {"trigger_id": trigger})
                    assert any(b["id"] == binding["id"] and b["owner"]["user_id"] == trace["identities"]["owner"] for b in listed)
                    for plural, identifier in (("channels", channel), ("triggers", trigger)):
                        access = (await owner.get(f"/v1/{plural}/{identifier}/access")).json()
                        response = await owner.put(f"/v1/{plural}/{identifier}/access", json={
                            "owners": [trace["identities"]["owner"]], "grants": {trace["identities"]["reader"]: "read"}, "public": False, "rev": access["rev"],
                        })
                        assert response.status_code == 200, response.text
                    for scoped, args in (
                        ("system__create_channel_binding", binding_args),
                        ("system__delete_channel_binding", {"trigger_id": trigger, "subscription_id": binding["id"]}),
                        ("trigger__fire_now", {"id": trigger}),
                    ):
                        error = await call(scoped, args, role="reader", error="tool_failed")
                        assert "forbidden" in error["error"]["message"]
                    assert len(await call("system__list_channel_bindings", {"trigger_id": trigger})) == 1
                    await call("system__set_reply_binding", {"workspace_id": workspace, "channel_id": channel})
                    target = await call("workspaces__get_workspace", {"id": workspace})
                    assert target["reply_binding"]["channel_id"] == channel
                    trace["standing"] = target["reply_binding"]
                    diagnostic = await call("trigger__fire_now", {"id": trigger})
                    assert diagnostic["results"] and all(r["skipped"] and r["error_code"] == "skipped_no_match" for r in diagnostic["results"])
                    trace["manual_fire"] = diagnostic

                    async def session_list():
                        page = await call("workspaces__list_workspace_sessions", {"workspace_id": workspace, "limit": 200})
                        return [read_session_info(item, workspace, responder) for item in page["items"]]

                    assert await session_list() == []
                    raw = {"channel": external, "user": "UHUMAN", "ts": "1731600001.000002", "thread_ts": "1731600001.000001",
                           "text": "help: Cedar " + unique_suffix}
                    response = await admin.post("/__fixture__/channel-event/" + channel, json=raw)
                    assert response.status_code == 409  # no lazy adapter construction by the injection fixture
                    activation_before = response.status_code
                    response = await owner.put("/v1/channels/" + channel, json=room_body)
                    assert response.status_code == 200 and response.json()["access"]["owners"] == room["access"]["owners"], response.text
                    trace["activation"] = {"pre_http": activation_before, "save_http": response.status_code, "saved": response.json()}
                    trace["checks"]["human_activation_required"] = True
                    unmatched = {**raw, "ts": "1731600002.000002", "thread_ts": "1731600002.000001", "text": "not a help request"}
                    response = await admin.post("/__fixture__/channel-event/" + channel, json=unmatched)
                    assert response.status_code == 200 and response.json()["routed"] is True
                    assert await session_list() == [] and not [q for q in wire.requests if q["method"] == "chat.postMessage"]
                    trace["unmatched"] = {"event": unmatched, "sessions": [], "posts": []}
                    trace["checks"]["nonmatching_message_has_no_effect"] = True
                    response = await admin.post("/__fixture__/channel-event/" + channel, json=raw)
                    assert response.status_code == 200 and response.json()["routed"] is True
                    trace["inbound"] = raw
                    spawned = await session_list()
                    assert len(spawned) == 1
                    inbound_sid = spawned[0].session_id

                    async def finished(sid, text, *, anchor=None):
                        while asyncio.get_running_loop().time() < deadline:
                            response = await owner.get(f"/v1/sessions/{sid}")
                            assert response.status_code == 200
                            row = response.json()
                            assert row["initiated_by"]["user_id"] == trace["identities"]["owner"] and row["initiated_by"]["role"] == "user"
                            assert row["ended_reason"] not in {"failed", "cancelled"}
                            posts = [q["payload"] for q in wire.requests if q["method"] == "chat.postMessage" and "CHANNEL:" + text in q["payload"].get("text", "")]
                            if row["turn_status"] == "idle" and row["ended_reason"] == "completed" and posts:
                                break
                            await asyncio.sleep(.1)
                        else:
                            pytest.fail("actual worker channel reply never completed")
                        assert all(post["channel"] == external and post.get("thread_ts") for post in posts)
                        if anchor is not None:
                            assert all(post["thread_ts"] == anchor for post in posts)
                        items = (await owner.get(f"/v1/sessions/{sid}/messages", params={"limit": 200})).json()["items"]
                        records = transcript_records(items)
                        answer, value = stage_answer(records)
                        assert value == {"received": text, "answer": "CHANNEL:" + text}
                        native = await call("workspaces__get_workspace_session", {"workspace_id": workspace, "session_id": sid})
                        info = read_session_info(native["info"], workspace, responder)
                        assert info.session_id == sid and info.ended_reason == "completed"
                        assert native["status"] == info.status.value == "ended"
                        return {"id": sid, "row": row, "items": items, "records": records, "answer": answer, "value": value, "posts": posts, "native_view": native}

                    inbound = await finished(inbound_sid, raw["text"], anchor=raw["thread_ts"])
                    assert inbound["row"]["metadata"]["trigger_id"] == trigger
                    assert inbound["row"]["metadata"]["subscription_id"] == binding["id"]
                    assert inbound["row"]["metadata"]["reply_binding"]["anchor"] == raw["thread_ts"]
                    trace["inbound_session"] = inbound
                    manual_text = "manual: Cedar " + unique_suffix
                    response = await owner.post("/v1/workspaces/" + workspace + "/sessions", json={
                        "binding": {"kind": "agent", "agent_id": responder}, "initial_instructions": manual_text, "auto_start": True, "autonomous": True,
                    })
                    assert response.status_code == 201, response.text
                    manual = await finished(response.json()["id"], manual_text)
                    assert not manual["row"]["metadata"].get("reply_binding")
                    assert manual["posts"][0]["thread_ts"] != raw["thread_ts"]
                    trace["manual_session"] = manual
                    await call("system__delete_channel_binding", {"trigger_id": trigger, "subscription_id": binding["id"]})
                    assert await call("system__list_channel_bindings", {"trigger_id": trigger}) == []
                    await call("system__clear_reply_binding", {"workspace_id": workspace})
                    assert (await call("workspaces__get_workspace", {"id": workspace}))["reply_binding"] is None
                    await stage("delete")
                    assert trigger not in {t["id"] for t in await trigger_rows()}
                    trace["cleanup"] = []
                    for plural, identifier, client in (("workspaces", workspace, owner), ("channels", channel, owner), ("channel_providers", provider, admin)):
                        response = await client.delete(f"/v1/{plural}/{identifier}")
                        assert response.status_code == 204, response.text
                        reread = await client.get(f"/v1/{plural}/{identifier}")
                        assert reread.status_code == 404
                        trace["cleanup"].append({"kind": plural, "id": identifier, "delete_http": response.status_code, "get_http": reread.status_code})
                    after = (await owner.get("/v1/workspaces/" + defaults["owner"])).json()
                    assert after["is_default"] and after["access"] == default_before["access"]
                    assert (tmp_path / "workspaces" / defaults["owner"]).is_dir()
                    trace["default"] = {"before": default_before, "after": after, "directory_exists": True}
                    trace["checks"].update(reader_writes_refused=True, binding_removed=True, trigger_deleted=True,
                                           standing_cleared=True, owned_fixtures_deleted=True, default_preserved=True)
                    trace["wire"] = wire.requests
                    trace["outcome"] = "passed"
                    require_evidence(trace, trace["source_sha"], page_hash)
        finally:
            if process is not None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
            trace["wire"] = wire.requests
            (evidence / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")


def require_evidence(trace: dict, source: str, page_hash: str) -> None:
    from primer.bootstrap.seed import default_policy_id
    from primer.model.chat import Message, ToolCallPart

    assert trace["case"] == CASE and trace["outcome"] == "passed"
    assert trace["source_sha"] == source and trace["runbook_hash"] == page_hash
    ids = trace["identities"]
    assert set(ids) == {"owner", "reader", "stranger"} and len(set(ids.values())) == 3
    assert trace["approver"] not in ids.values()
    assert set(trace["stages"]) == set(SPECS) and [g["stage"] for g in trace["gates"]] == list(SPECS)
    assert len({g["gate_id"] for g in trace["gates"]}) == len(SPECS)
    for g in trace["gates"]:
        name = g["stage"];operation, actor, decision = SPECS[name];s = trace["stages"][name]
        assert g["operation"] == operation and g["decision"] == decision and g["http"] == 202
        assert g["policy_id"] == g["pending"]["policy_id"] == default_policy_id("trigger", operation)
        assert g["gate_id"] == g["pending"]["gate_id"] and g["pending"]["approval_type"] == "required"
        assert g["pending"]["tool_name"] == operation and g["pending"]["arguments"] == trace["actions"][name]
        assert s["actor"] == actor and s["initiator"] == ids[actor]
        assert s["started"]["initiated_by"]["user_id"] == ids[actor] and s["started"]["initiated_by"]["role"] == "user"
        records, answer, value = read_gate(s["items"], name)
        assert s["records"] == records and s["answer"] == answer and s["result"] == value
        calls = [p for row in s["items"] if "kind" not in row for p in Message.model_validate(row).parts if isinstance(p, ToolCallPart)]
        assert len(calls) == 1 and calls[0].id == name and calls[0].name == "system__call_tool"
        assert calls[0].arguments == {"toolset_id": "trigger", "tool_name": operation, "arguments": trace["actions"][name]}
        if name in {"reject_create", "denied_create"}:
            assert s["before"] == s["after"]
    created = trace["stages"]["create"]["result"]
    assert created["id"] == trace["trigger"] and created["access"]["owners"] == [ids["owner"]]
    assert created["config"] == trace["actions"]["create"]["config"]
    assert created["config"]["provider_id"] == trace["provider"] and created["config"]["channel_id"] == trace["channel"]
    assert any(r["id"] == trace["trigger"] and r["config"] == created["config"] for r in trace["stages"]["create"]["after"])
    assert trace["trigger"] not in {r["id"] for r in trace["stages"]["delete"]["after"]}
    assert created["owner"]["user_id"] == ids["owner"] and trace["actions"]["delete"] == {"id": trace["trigger"]}
    binding = trace["binding"]
    assert binding["owner"]["type"] == "user" and binding["owner"]["role"] == "user" and binding["owner"]["user_id"] == ids["owner"]
    assert binding["trigger_id"] == trace["trigger"] and binding["config"] == trace["binding_arguments"]["config"]
    assert binding["config"]["workspace_id"] == trace["workspace"] and binding["reply_target"] == "source_thread"
    assert binding["event_matcher"]["event_type"] == "message.posted" and binding["event_matcher"]["text_pattern"] == "^help:"
    assert binding["payload_template"] == "{{ event.text }}" and binding["enabled"] is True
    assert trace["provider_readback"]["id"] == trace["provider"]
    assert all(set(trace["provider_readback"]["config"][field]) == {"*"} for field in ("app_token", "bot_token"))
    assert trace["manual_fire"]["results"] and all(r["skipped"] and r["error_code"] == "skipped_no_match" for r in trace["manual_fire"]["results"])
    for key, prefix in (("inbound_session", "help:"), ("manual_session", "manual:")):
        session = trace[key]
        info = read_session_info(session["native_view"]["info"], trace["workspace"], binding["config"]["agent_id"])
        assert info.session_id == session["id"] and info.ended_reason == "completed"
        assert session["native_view"]["status"] == info.status.value == "ended"
        assert session["row"]["workspace_id"] == trace["workspace"]
        assert session["row"]["binding"]["agent_id"] == binding["config"]["agent_id"]
        assert session["row"]["initiated_by"]["user_id"] == ids["owner"] and session["row"]["initiated_by"]["role"] == "user"
        assert session["row"]["turn_status"] == "idle" and session["row"]["ended_reason"] == "completed"
        records = transcript_records(session["items"]);answer, value = stage_answer(records)
        assert records == session["records"] and answer == session["answer"] and value == session["value"]
        assert value["received"].startswith(prefix) and value["answer"] == "CHANNEL:" + value["received"]
        assert session["posts"] and all(p["channel"] == trace["external"] and value["answer"] in p["text"] for p in session["posts"])
        assert all(p in [q["payload"] for q in trace["wire"] if q["method"] == "chat.postMessage"] for p in session["posts"])
    incoming = trace["inbound_session"]
    assert incoming["id"] != trace["manual_session"]["id"]
    assert incoming["value"]["received"] == trace["inbound"]["text"]
    assert incoming["row"]["metadata"]["trigger_id"] == trace["trigger"] and incoming["row"]["metadata"]["subscription_id"] == binding["id"]
    assert incoming["row"]["metadata"]["reply_binding"]["channel_id"] == trace["channel"]
    assert incoming["row"]["metadata"]["reply_binding"]["anchor"] == trace["inbound"]["thread_ts"]
    assert all(p["thread_ts"] == trace["inbound"]["thread_ts"] for p in incoming["posts"])
    assert trace["standing"]["channel_id"] == trace["channel"]
    assert trace["activation"]["pre_http"] == 409 and trace["activation"]["save_http"] == 200
    assert trace["activation"]["saved"]["id"] == trace["channel"] and trace["activation"]["saved"]["access"]["owners"] == [ids["owner"]]
    assert trace["unmatched"]["sessions"] == trace["unmatched"]["posts"] == []
    assert not trace["unmatched"]["event"]["text"].startswith("help:")
    readers = [c for c in trace["calls"] if c["actor"] == "reader"]
    assert {c["scoped_id"] for c in readers} == {"system__create_channel_binding", "system__delete_channel_binding", "trigger__fire_now"}
    assert len(readers) == 3 and all(c["envelope"]["ok"] is False and "forbidden" in c["envelope"]["error"]["message"] for c in readers)
    default = trace["default"]
    assert default["before"]["id"] == default["after"]["id"] and default["after"]["id"] != trace["workspace"]
    assert default["before"]["is_default"] is True and default["after"]["is_default"] is True
    assert default["before"]["access"] == default["after"]["access"] and default["directory_exists"] is True
    assert {(c["kind"], c["id"]) for c in trace["cleanup"]} == {
        ("workspaces", trace["workspace"]), ("channels", trace["channel"]), ("channel_providers", trace["provider"]),
    }
    assert len(trace["cleanup"]) == 3 and all(c["delete_http"] == 204 and c["get_http"] == 404 for c in trace["cleanup"])
    assert not trace["manual_session"]["row"]["metadata"].get("reply_binding")
    assert all(p.get("thread_ts") and p["thread_ts"] != trace["inbound"]["thread_ts"] for p in trace["manual_session"]["posts"])
    assert set(trace["checks"]) == {"human_activation_required", "nonmatching_message_has_no_effect", "reader_writes_refused",
                                    "binding_removed", "trigger_deleted", "standing_cleared", "owned_fixtures_deleted", "default_preserved"}
    assert all(v is True for v in trace["checks"].values())
