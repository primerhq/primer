"""CI-owned trigger/subscription lifecycle against a private API+worker."""

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

from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for
from tests.e2e.test_wiki_w7_t1_lifecycle import _share, isolated_config, mcp_http_client, transcript_records
from tests.e2e.test_wiki_w7_t2_lifecycle import stage_answer
from tests.e2e.test_wiki_w7_t4_lifecycle import read_session_info

REPO = Path(__file__).resolve().parents[2]
PAGE = "runbooks/wire-a-trigger"
CASE = "test_user_wires_trigger_subscription_and_proves_actual_worker_answer"
SPECS = {
    "reject_create": ("create", "owner", "rejected"),
    "create": ("create", "owner", "approved"),
    "denied_update": ("update", "reader", "approved"),
    "disable": ("update", "owner", "approved"),
    "webhook": ("create", "owner", "approved"),
    "delete": ("delete", "owner", "approved"),
    "delete_webhook": ("delete", "owner", "approved"),
}
ALLOWED = {"trigger__" + n for n in (
    "create", "update", "delete", "get", "list", "fire_now", "create_subscription", "get_subscription",
    "list_subscriptions", "update_subscription", "delete_subscription",
)} | {"system__wiki_read", "system__describe_tool", "system__get_agent", "workspaces__create_workspace",
     "workspaces__get_workspace", "workspaces__list_workspace_sessions", "workspaces__get_workspace_session"}


def gate_result(name: str, value) -> None:
    if name == "reject_create":
        assert value["rejected"] is True and value["tool_name"] == "create"
    elif name == "denied_update":
        assert value["type"] == "forbidden"
    elif name in {"create", "webhook"}:
        assert value["id"] and value["config"]["kind"] == ("delayed" if name == "create" else "webhook")
        assert value["owner"]["type"] == "user" and value["owner"]["role"] == "user"
    elif name == "disable":
        assert value["id"] and value["enabled"] is False and value["next_fire_at"] is None
    elif name in {"delete", "delete_webhook"}:
        assert value == {"ok": True}
    else:
        raise AssertionError("unknown trigger gate")


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
        text = next((m.get("content") for m in reversed(request.get("messages", [])) if m.get("role") == "user"), None)
        if not isinstance(text, str) or not text.startswith("TRIGGER:"):
            return False
        self.emit_text = "RESULT:" + json.dumps({"received": text, "answer": "PROVED:" + text})
        return True


def read_gate(items: list[dict], name: str):
    from primer.model.chat import Message, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord

    failed = name in {"reject_create", "denied_update"}
    records, errors = [], []
    for row in items:
        if "kind" in row:
            SessionMessageRecord.model_validate(row)
            records.append(row)
        else:
            for part in Message.model_validate(row).parts:
                if isinstance(part, ToolResultPart) and part.error:
                    assert failed and part.id == name
                    gate_result(name, json.loads(part.output))
                    errors.append(part)
    assert len(errors) == int(failed)
    event_errors = [r for r in records if r["kind"] == "tool_result" and r["payload"].get("error")]
    assert len(event_errors) == int(failed)
    if event_errors:
        gate_result(name, json.loads(event_errors[0]["payload"]["output"]))
    answer, result = stage_answer(records)
    gate_result(name, result)
    raw = [p for row in items if "kind" not in row for p in Message.model_validate(row).parts if isinstance(p, ToolResultPart)]
    assert any(p.id == name and p.error is failed and json.loads(p.output) == result for p in raw)
    return records, answer, result


def prepare_config(root: Path, port: int) -> tuple[Path, dict]:
    wiki = root / "wiki"
    (wiki / "runbooks").mkdir(parents=True)
    (wiki / "index.md").write_text("---\ntitle: T5 acceptance\nsummary: Private trigger flow.\n---\n[Runbooks](runbooks)\n")
    (wiki / "runbooks/index.md").write_text("---\ntitle: T5 runbook\nsummary: Private trigger flow.\n---\n[Wire a trigger](" + PAGE + ")\n")
    shutil.copyfile(REPO / "docs/wiki" / (PAGE + ".md"), wiki / (PAGE + ".md"))
    config = root / "config.json"
    config.write_text(json.dumps(isolated_config(root, port)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRIMER_")}
    env.update(PRIMER_WIKI_DIR=str(wiki))
    return config, env


def start_server(root: Path, port: int, log):
    config, env = prepare_config(root, port)
    return subprocess.Popen([sys.executable, "-m", "primer", "api", "--config", str(config)],
                            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)


@pytest.mark.asyncio
async def test_user_wires_trigger_subscription_and_proves_actual_worker_answer(mock_llm, unique_suffix, tmp_path, unused_tcp_port):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    registry, llm_url = mock_llm
    model, llm, driver, responder = "w7-t5-" + unique_suffix, "w7-llm", "w7-driver", "w7-responder"
    profile, workspace = profile_id_for(llm, model), "w7-work-" + unique_suffix
    config = {"kind": "delayed", "fire_at": "2099-01-01T09:00:00Z"}
    actions = {n: {"slug": "w7-" + n.replace("_", "-") + "-" + unique_suffix, "name": n, "config": config}
               for n in ("reject_create", "create")}
    actions.update(denied_update={}, disable={}, webhook={"slug": "w7-webhook-" + unique_suffix, "name": "webhook", "config": {"kind": "webhook"}},
                   delete={}, delete_webhook={})
    registry.register(model, [*[GateRule(n, actions[n]) for n in SPECS], ReplyRule()])
    evidence = REPO / "tests/.e2e/w7-t5"
    evidence.mkdir(parents=True, exist_ok=True)
    page_hash = hashlib.sha256((REPO / "docs/wiki" / (PAGE + ".md")).read_bytes()).hexdigest()
    trace = {"case": CASE, "source_sha": os.environ.get("GITHUB_SHA", "local"), "runbook_hash": page_hash, "outcome": "running",
             "identities": {}, "calls": [], "gates": [], "stages": {}, "checks": {}, "workspace": workspace, "actions": actions}
    process = None
    try:
        with (evidence / "server.stdout").open("wb") as log:
            process = start_server(tmp_path, unused_tcp_port, log)
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
                    pytest.fail("private trigger child never became healthy")
                for verb in ("register", "login"):
                    response = await admin.post("/v1/auth/" + verb, json={"username": "w7-admin", "password": "private-w7-password-123"})
                    assert response.status_code in (200, 201), response.text
                status = (await admin.get("/v1/auth/status")).json()
                assert status["role"] == "admin"
                trace["approver"] = status["principal"]["user_id"]
                setup = [
                    ("workspace_providers", {"id": "w7-local", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path / "workspaces")}}),
                    ("workspace_templates", {"id": "w7-template", "description": "Private T5 template", "provider_id": "w7-local", "backend": {"kind": "local"}}),
                    ("llm_providers", {"id": llm, "provider": "openchat", "config": {"url": llm_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 2}}),
                    ("model_profiles", {"id": profile, "description": "T5 scripted model", "provider_id": llm, "model_name": model, "context_length": 32000}),
                ]
                for aid, granted in ((driver, ["system__call_tool"]), (responder, [])):
                    setup.append(("agents", {"id": aid, "description": "T5 private " + aid, "model": {"profile_id": profile},
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

                trace["wiki"] = await call("system__wiki_read", {"path": PAGE})
                assert (REPO / "docs/wiki" / (PAGE + ".md")).read_text().split("---", 2)[2].strip() in trace["wiki"]["body"]
                target = await call("workspaces__create_workspace", {"id": workspace, "template_id": "w7-template"})
                assert target["access"]["owners"] == [trace["identities"]["owner"]]
                for scoped, args in (("workspaces__get_workspace", {"id": workspace}), ("system__get_agent", {"id": responder})):
                    assert (await call(scoped, args))["id"] == args["id"]
                policies = {}
                for name in ("create", "update", "delete"):
                    detail = await call("system__describe_tool", {"toolset_id": "trigger", "tool_name": name})
                    assert detail["required_role"] == "user" and detail["approval"]["type"] == "required" and detail["approval"]["gated"]
                    policies[name] = detail["approval"]["policy_id"]
                await call("trigger__create", actions["create"], error="tool_unavailable")
                deadline = asyncio.get_running_loop().time() + 100

                async def trigger_rows():
                    return await call("trigger__list", {})

                async def grant_read(identifier):
                    path = "/v1/triggers/" + identifier + "/access"
                    view = (await owner.get(path)).json()
                    response = await owner.put(path, json={"owners": [trace["identities"]["owner"]],
                        "grants": {trace["identities"]["reader"]: "read"}, "public": False, "rev": view["rev"]})
                    assert response.status_code == 200, response.text

                async def session_list():
                    page = await call("workspaces__list_workspace_sessions", {"workspace_id": workspace, "limit": 200})
                    return [read_session_info(item, workspace, responder) for item in page["items"]]

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
                        pytest.fail("trigger gate never parked: " + name)
                    assert pending["tool_name"] == operation and pending["approval_type"] == "required"
                    assert pending["policy_id"] == policies[operation] and pending["arguments"] == actions[name] and pending["gate_id"]
                    assert await trigger_rows() == before
                    response = await admin.post(f"/v1/sessions/{sid}/tool_approval/respond", json={
                        "tool_call_id": pending["tool_call_id"], "gate_id": pending["gate_id"], "decision": decision, "reason": "Private T5 " + name,
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
                        pytest.fail("trigger gate never completed: " + name)
                    items = (await admin.get(f"/v1/sessions/{sid}/messages", params={"limit": 200})).json()["items"]
                    records, answer, value = read_gate(items, name)
                    after = await trigger_rows()
                    if name in {"reject_create", "denied_update"}:
                        assert before == after
                    trace["gates"].append({"stage": name, "operation": operation, "decision": decision, "http": 202,
                                           "policy_id": pending["policy_id"], "gate_id": pending["gate_id"], "pending": pending})
                    trace["stages"][name] = {"actor": role, "initiator": trace["identities"][role], "started": started, "session_id": sid,
                                             "before": before, "after": after, "items": items, "records": records, "answer": answer, "result": value}
                    return value

                await stage("reject_create")
                trigger = (await stage("create"))["id"]
                trace["trigger"] = trigger
                assert (await call("trigger__get", {"id": trigger}))["owner"]["user_id"] == trace["identities"]["owner"]
                actions["denied_update"].update(id=trigger, enabled=False)
                actions["disable"].update(id=trigger, enabled=False)
                actions["delete"]["id"] = trigger
                await grant_read(trigger)
                # Give the reader full target reach; only trigger READ is the write refusal under test.
                access = (await owner.get(f"/v1/workspaces/{workspace}/access")).json()
                response = await owner.put(f"/v1/workspaces/{workspace}/access", json={"owners": [trace["identities"]["owner"]],
                    "grants": {trace["identities"]["reader"]: "full"}, "public": False, "rev": access["rev"]})
                assert response.status_code == 200, response.text
                await stage("denied_update")
                hidden = await call("trigger__get", {"id": trigger}, role="stranger", error="tool_failed")
                assert "trigger_not_found" in hidden["error"]["message"]
                trace["hidden"] = hidden
                subargs = {"trigger_id": trigger, "config": {"kind": "agent_fresh_session", "workspace_id": workspace, "agent_id": responder},
                           "payload_template": "not enabled", "parallelism": "queue", "enabled": False}
                sub = await call("trigger__create_subscription", subargs)
                assert sub["owner"]["user_id"] == trace["identities"]["owner"] and sub["owner"]["role"] == "user"
                trace["subscription_created"] = sub
                subid = {"trigger_id": trigger, "subscription_id": sub["id"]}
                assert (await call("trigger__get_subscription", subid))["id"] == sub["id"]
                assert [s["id"] for s in await call("trigger__list_subscriptions", {"trigger_id": trigger})] == [sub["id"]]
                assert await session_list() == []
                no_delivery = await call("trigger__fire_now", {"id": trigger})
                assert no_delivery["skipped"] is False and no_delivery["fire_id"] and no_delivery["results"] == []
                assert await session_list() == []
                trace["disabled_subscription_fire"] = no_delivery
                payload = "TRIGGER:" + unique_suffix + " {{ fire_id }} {{ trigger_id }}"
                updateargs = {**subid, "enabled": True, "payload_template": payload, "parallelism": "queue"}
                enabled = await call("trigger__update_subscription", updateargs)
                assert enabled["enabled"] is True and enabled["payload_template"] == payload
                trace["subscription"] = await call("trigger__get_subscription", subid)
                trace["payload_template"] = payload
                before = (await owner.get(f"/v1/triggers/{trigger}")).json()
                before_sub = await call("trigger__get_subscription", subid)
                for scoped, args in (("trigger__create_subscription", subargs), ("trigger__update_subscription", updateargs),
                                     ("trigger__delete_subscription", subid), ("trigger__fire_now", {"id": trigger})):
                    error = await call(scoped, args, role="reader", error="tool_failed")
                    assert "forbidden" in error["error"]["message"]
                assert (await owner.get(f"/v1/triggers/{trigger}")).json() == before
                assert await call("trigger__get_subscription", subid) == before_sub and await session_list() == []
                trace["reader_no_effect"] = {"before": before, "after": (await owner.get(f"/v1/triggers/{trigger}")).json(),
                                              "sub_before": before_sub, "sub_after": await call("trigger__get_subscription", subid)}
                fire = await call("trigger__fire_now", {"id": trigger})
                assert fire["skipped"] is False and fire["fire_id"] and len(fire["results"]) == 1
                delivery = fire["results"][0]
                assert delivery["ok"] is True and delivery["skipped"] is False and delivery["error_code"] is None
                assert delivery["subscription_id"] == sub["id"] and delivery["artefact_id"]
                sid = delivery["artefact_id"]
                expected = "TRIGGER:" + unique_suffix + " " + fire["fire_id"] + " " + trigger
                while asyncio.get_running_loop().time() < deadline:
                    response = await owner.get(f"/v1/sessions/{sid}")
                    assert response.status_code == 200, response.text
                    row = response.json()
                    assert row["initiated_by"]["user_id"] == trace["identities"]["owner"] and row["initiated_by"]["role"] == "user"
                    assert row["ended_reason"] not in {"failed", "cancelled"}
                    if row["turn_status"] == "idle" and row["ended_reason"] == "completed":
                        break
                    await asyncio.sleep(.1)
                else:
                    pytest.fail("dispatched trigger worker never completed")
                items = (await owner.get(f"/v1/sessions/{sid}/messages", params={"limit": 200})).json()["items"]
                records = transcript_records(items);answer, value = stage_answer(records)
                assert value == {"received": expected, "answer": "PROVED:" + expected}
                listed = await session_list()
                assert [i.session_id for i in listed] == [sid]
                native = await call("workspaces__get_workspace_session", {"workspace_id": workspace, "session_id": sid})
                info = read_session_info(native["info"], workspace, responder)
                assert info.session_id == sid and info.ended_reason == "completed" and native["status"] == info.status.value == "ended"
                assert row["metadata"]["trigger_id"] == trigger and row["metadata"]["subscription_id"] == sub["id"]
                assert row["metadata"]["fire_id"] == fire["fire_id"] and row["initial_instructions"] == expected
                outcome = await call("trigger__get_subscription", subid)
                assert outcome["last_fired_at"] and outcome["last_fire_error"] is None
                trace["fire"] = fire
                trace["actual_run"] = {"id": sid, "row": row, "items": items, "records": records, "answer": answer, "value": value,
                                       "native_view": native, "listed": [i.model_dump(mode="json") for i in listed], "subscription_outcome": outcome}
                await stage("disable")
                disabled = await call("trigger__fire_now", {"id": trigger})
                assert disabled == {"skipped": True, "fire_id": None, "results": []}
                assert [i.session_id for i in await session_list()] == [sid]
                trace["disabled_trigger_fire"] = disabled
                assert await call("trigger__delete_subscription", subid) == {"ok": True}
                assert await call("trigger__list_subscriptions", {"trigger_id": trigger}) == []
                missing = await call("trigger__get_subscription", subid, error="tool_failed")
                assert "subscription_not_found" in missing["error"]["message"]
                trace["subscription_deleted"] = {"arguments": subid, "missing": missing, "list": []}
                webhook = await stage("webhook")
                await grant_read(webhook["id"])
                owner_view = await call("trigger__get", {"id": webhook["id"]})
                admin_view = await call("trigger__get", {"id": webhook["id"]}, role="admin")
                reader_view = await call("trigger__get", {"id": webhook["id"]}, role="reader")
                reader_list = await call("trigger__list", {"kind": "webhook"}, role="reader")
                from primer.trigger.owner import WEBHOOK_TOKEN_MASK

                assert len(owner_view["config"]["token"]) == 32 and owner_view["config"]["token"] == admin_view["config"]["token"]
                assert reader_view["config"]["token"] == WEBHOOK_TOKEN_MASK
                assert [t["config"]["token"] for t in reader_list if t["id"] == webhook["id"]] == [WEBHOOK_TOKEN_MASK]
                assert set(reader_view["owner"]) == {"display", "role"}
                trace["webhook_views"] = {"owner": owner_view, "admin": admin_view, "reader": reader_view, "reader_list": reader_list}
                actions["delete_webhook"]["id"] = webhook["id"]
                await stage("delete")
                await stage("delete_webhook")
                assert trigger not in {r["id"] for r in await trigger_rows()} and webhook["id"] not in {r["id"] for r in await trigger_rows()}
                trace["remaining_triggers"] = await trigger_rows()
                response = await owner.delete("/v1/workspaces/" + workspace)
                assert response.status_code == 204, response.text
                reread = await owner.get("/v1/workspaces/" + workspace)
                assert reread.status_code == 404
                after_default = (await owner.get("/v1/workspaces/" + defaults["owner"])).json()
                assert after_default["is_default"] and after_default["access"] == default_before["access"]
                assert (tmp_path / "workspaces" / defaults["owner"]).is_dir()
                trace["cleanup"] = {"workspace": workspace, "delete_http": response.status_code, "get_http": reread.status_code}
                trace["default"] = {"before": default_before, "after": after_default, "directory_exists": True}
                trace["checks"] = {k: True for k in ("rejection_no_effect", "approved_reader_refused", "disabled_subscription_no_delivery",
                    "reader_writes_no_effect", "actual_worker_answer", "disabled_trigger_no_delivery", "subscription_removed",
                    "webhook_token_visibility", "owned_triggers_removed", "workspace_removed", "default_preserved")}
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
        (evidence / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")


def require_evidence(trace: dict, source: str, page_hash: str) -> None:
    from primer.bootstrap.seed import default_policy_id
    from primer.model.chat import Message, ToolCallPart
    from primer.trigger.owner import WEBHOOK_TOKEN_MASK
    from primer.trigger.payload import render_payload

    assert trace["case"] == CASE and trace["outcome"] == "passed"
    assert trace["source_sha"] == source and trace["runbook_hash"] == page_hash
    assert trace["wiki"]["path"] == PAGE
    assert (REPO / "docs/wiki" / (PAGE + ".md")).read_text().split("---", 2)[2].strip() in trace["wiki"]["body"]
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
        if name in {"reject_create", "denied_update"}:
            assert s["before"] == s["after"]
        if name in {"create", "webhook"}:
            assert value["owner"]["user_id"] == ids["owner"] and value["access"]["owners"] == [ids["owner"]]
            assert any(r["id"] == value["id"] and r["config"] == value["config"] for r in s["after"])
    created = trace["stages"]["create"]["result"]
    assert created["id"] == trace["trigger"] and created["enabled"] is True
    assert created["config"] == trace["actions"]["create"]["config"] == {"kind": "delayed", "fire_at": "2099-01-01T09:00:00Z"}
    assert trace["actions"]["disable"] == trace["actions"]["denied_update"] == {"id": trace["trigger"], "enabled": False}
    assert trace["stages"]["disable"]["result"]["id"] == trace["trigger"]
    sub = trace["subscription"]
    assert trace["subscription_created"]["id"] == sub["id"] and trace["subscription_created"]["enabled"] is False
    assert sub["trigger_id"] == trace["trigger"] and sub["enabled"] is True and sub["parallelism"] == "queue"
    assert sub["owner"]["type"] == "user" and sub["owner"]["role"] == "user" and sub["owner"]["user_id"] == ids["owner"]
    assert sub["config"]["kind"] == "agent_fresh_session" and sub["config"]["workspace_id"] == trace["workspace"]
    assert sub["payload_template"] == trace["payload_template"]
    empty = trace["disabled_subscription_fire"]
    assert empty["skipped"] is False and empty["fire_id"] and empty["results"] == []
    assert trace["disabled_trigger_fire"] == {"skipped": True, "fire_id": None, "results": []}
    no_effect = trace["reader_no_effect"]
    assert no_effect["before"] == no_effect["after"] and no_effect["sub_before"] == no_effect["sub_after"]
    readers = [c for c in trace["calls"] if c["actor"] == "reader" and c["scoped_id"] != "trigger__get" and c["scoped_id"] != "trigger__list"]
    assert {c["scoped_id"] for c in readers} == {"trigger__create_subscription", "trigger__update_subscription", "trigger__delete_subscription", "trigger__fire_now"}
    assert len(readers) == 4 and all(c["envelope"]["ok"] is False and "forbidden" in c["envelope"]["error"]["message"] for c in readers)
    assert trace["hidden"]["ok"] is False and "trigger_not_found" in trace["hidden"]["error"]["message"]
    fire, run = trace["fire"], trace["actual_run"]
    assert fire["skipped"] is False and fire["fire_id"] and len(fire["results"]) == 1
    result = fire["results"][0]
    assert result["subscription_id"] == sub["id"] and result["artefact_id"] == run["id"]
    assert result["ok"] is True and result["skipped"] is False and result["error_code"] is None
    row = run["row"]
    assert row["id"] == run["id"] and row["workspace_id"] == trace["workspace"] and row["binding"]["agent_id"] == sub["config"]["agent_id"]
    assert row["initiated_by"]["user_id"] == ids["owner"] and row["initiated_by"]["role"] == "user"
    assert row["turn_status"] == "idle" and row["ended_reason"] == "completed"
    assert row["metadata"]["trigger_id"] == trace["trigger"] and row["metadata"]["subscription_id"] == sub["id"] and row["metadata"]["fire_id"] == fire["fire_id"]
    expected = render_payload(sub["payload_template"], {"fire_id": fire["fire_id"], "trigger_id": trace["trigger"]})
    assert expected.startswith("TRIGGER:") and expected == row["initial_instructions"]
    records = transcript_records(run["items"]);answer, value = stage_answer(records)
    assert records == run["records"] and answer == run["answer"] and value == run["value"]
    assert value == {"received": expected, "answer": "PROVED:" + expected}
    native = run["native_view"]
    info = read_session_info(native["info"], trace["workspace"], sub["config"]["agent_id"])
    assert info.session_id == run["id"] and info.ended_reason == "completed" and native["status"] == info.status.value == "ended"
    assert [read_session_info(i, trace["workspace"], sub["config"]["agent_id"]).session_id for i in run["listed"]] == [run["id"]]
    outcome = run["subscription_outcome"]
    assert outcome["id"] == sub["id"] and outcome["last_fired_at"] and outcome["last_fire_error"] is None
    lists = [json.loads(c["envelope"]["output"])["items"] for c in trace["calls"] if c["scoped_id"] == "workspaces__list_workspace_sessions"]
    assert len(lists) == 5 and lists[:3] == [[], [], []]
    assert all([i["session_id"] for i in items] == [run["id"]] for items in lists[3:])
    removed = trace["subscription_deleted"]
    assert removed["arguments"] == {"trigger_id": trace["trigger"], "subscription_id": sub["id"]} and removed["list"] == []
    assert removed["missing"]["ok"] is False and "subscription_not_found" in removed["missing"]["error"]["message"]
    views = trace["webhook_views"];webhook = trace["stages"]["webhook"]["result"]
    assert webhook["config"]["kind"] == "webhook"
    assert all(v["id"] == webhook["id"] for k, v in views.items() if k != "reader_list")
    assert len(views["owner"]["config"]["token"]) == 32 and views["owner"]["config"]["token"] == views["admin"]["config"]["token"] == webhook["config"]["token"]
    assert views["reader"]["config"]["token"] == WEBHOOK_TOKEN_MASK and set(views["reader"]["owner"]) == {"display", "role"}
    assert [v["config"]["token"] for v in views["reader_list"] if v["id"] == webhook["id"]] == [WEBHOOK_TOKEN_MASK]
    for name, identifier in (("delete", trace["trigger"]), ("delete_webhook", webhook["id"])):
        assert trace["actions"][name] == {"id": identifier}
        assert identifier not in {r["id"] for r in trace["stages"][name]["after"]}
        assert identifier not in {r["id"] for r in trace["remaining_triggers"]}
    assert trace["cleanup"] == {"workspace": trace["workspace"], "delete_http": 204, "get_http": 404}
    default = trace["default"]
    assert default["before"]["id"] == default["after"]["id"] and default["after"]["id"] != trace["workspace"]
    assert default["before"]["is_default"] is True and default["after"]["is_default"] is True
    assert default["before"]["access"] == default["after"]["access"] and default["directory_exists"] is True
    assert set(trace["checks"]) == {"rejection_no_effect", "approved_reader_refused", "disabled_subscription_no_delivery",
        "reader_writes_no_effect", "actual_worker_answer", "disabled_trigger_no_delivery", "subscription_removed",
        "webhook_token_visibility", "owned_triggers_removed", "workspace_removed", "default_preserved"}
    assert all(v is True for v in trace["checks"].values())
