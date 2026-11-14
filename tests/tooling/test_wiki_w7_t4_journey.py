"""Safe local wire/isolation probes; actual channel acceptance is CI-owned."""

import json

import httpx
import pytest

from tests._support.mock_slack_wire import SlackWire, build_app


async def test_slack_wire_accepts_sdk_json_form_and_query_and_records_real_destination():
    wire = SlackWire()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(wire)), base_url="http://test") as client:
        response = await client.post("/api/auth.test", data={})
        assert response.json()["user_id"] == "UBOT"
        response = await client.get("/api/conversations.info", params={"channel": "CLOCAL"})
        assert response.json()["channel"] == {"id": "CLOCAL", "is_member": True}
        body = {"channel": "CLOCAL", "thread_ts": "1731600000.000001", "text": "Cedar actual answer"}
        response = await client.post("/api/chat.postMessage", json=body)
        assert response.json()["message"] == body and response.json()["ts"]
        assert wire.requests[-1] == {"method": "chat.postMessage", "payload": body}
        response = await client.post("/api/unknown")
        assert response.json() == {"ok": False, "error": "fixture_unknown_method"}
    assert all("token" not in str(r) for r in wire.requests)


@pytest.mark.parametrize("url", [
    "https://slack.com/api/", "http://127.0.0.1/api/", "http://127.0.0.1:50123/wrong/",
    "http://secret@127.0.0.1:50123/api/",
])
def test_child_transport_refuses_external_or_unbounded_slack_addresses(url):
    from tests._support.w7_channel_server import private_slack_transport

    with pytest.raises(AssertionError):
        private_slack_transport(url)


def test_explicit_child_uses_private_config_and_adds_only_a_fixture_event_boundary(tmp_path, monkeypatch):
    from primer.channel.slack import connection
    from tests._support.w7_channel_server import build_app as child_app
    from tests.e2e.test_wiki_w7_t1_lifecycle import isolated_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(isolated_config(tmp_path, 50124)))
    # The production child exits with its globals. This in-process factory
    # probe restores them explicitly and never enters lifespan or opens a socket.
    monkeypatch.setattr(connection, "_build_app_and_handler", connection._build_app_and_handler)
    monkeypatch.setattr(connection, "_build_outbound_app", connection._build_outbound_app)
    app, config = child_app(config_path, "http://127.0.0.1:50123/api/")
    assert config.auth.enabled and config.db.provider.value == "sqlite"
    assert str(config.db.config.path) == str(tmp_path / "data.sqlite")
    assert config.runtime_mode.value == "api+worker"
    assert "/__fixture__/channel-event/{channel_id}" in {getattr(r, "path", None) for r in app.routes}
    assert "/__fixture__/channel-event/{channel_id}" not in app.openapi()["paths"]
    assert not (tmp_path / "data.sqlite").exists()


def test_private_transport_keeps_native_slack_sdk_with_a_local_five_second_bound(monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    from primer.channel.slack import connection
    from primer.model.channel import SlackChannelProviderConfig
    from tests._support.w7_channel_server import private_slack_transport

    monkeypatch.setenv("HTTPS_PROXY", "http://do-not-use.invalid:1234")
    monkeypatch.setattr(connection, "_build_app_and_handler", connection._build_app_and_handler)
    monkeypatch.setattr(connection, "_build_outbound_app", connection._build_outbound_app)
    private_slack_transport("http://127.0.0.1:50123/api/")
    conn = connection._build_app_and_handler(SlackChannelProviderConfig(app_token="xapp-private", bot_token="xoxb-private"))
    assert conn.handler is None and isinstance(conn.app.client, AsyncWebClient)
    assert conn.app.client.base_url == "http://127.0.0.1:50123/api/"
    assert conn.app.client.proxy is None and conn.app.client.timeout == 5


async def test_journey_consumes_actual_workspace_list_and_get_session_info():
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from primer.mcp.server import current_actor
    from primer.model.principal import Principal
    from primer.model.workspace_session import AgentSessionBinding, SessionInfo, SessionStatus
    from primer.toolset.workspaces import build_workspaces_toolset
    from tests.e2e.test_wiki_w7_t4_lifecycle import read_session_info
    from tests.session._peer_fakes import make_world, seed_session, seed_workspace

    now = datetime(2026, 11, 15, tzinfo=UTC)
    world = make_world()
    await seed_workspace(world, "w", owners=["owner"])
    await seed_session(world, sid="actual-session", workspace_id="w", binding=AgentSessionBinding(agent_id="ag"),
                       status=SessionStatus.ENDED, ended_reason="completed", ended_at=now)
    # The real tools overlay the durable completion on a stale running slot.
    slot = SessionInfo(session_id="actual-session", agent_id="ag", workspace_id="w", status="running",
                       started_at=now, last_activity_at=now)
    session = SimpleNamespace(info=AsyncMock(return_value=slot))
    workspace = SimpleNamespace(list_sessions=AsyncMock(return_value=[slot]), get_session=AsyncMock(return_value=session))
    registry = SimpleNamespace(get_workspace=AsyncMock(return_value=workspace))
    tools = build_workspaces_toolset(storage_provider=world.sp, workspace_registry=registry)
    token = current_actor.set(Principal(type="user", id="owner", user_id="owner", display="owner", role="user", source="local"))
    try:
        listed = await tools.call(tool_name="list_workspace_sessions", arguments={"workspace_id": "w"})
        got = await tools.call(tool_name="get_workspace_session", arguments={"workspace_id": "w", "session_id": "actual-session"})
    finally:
        current_actor.reset(token)
    assert not listed.is_error and not got.is_error
    page, detail = json.loads(listed.output), json.loads(got.output)
    assert len(page["items"]) == page["total"] == 1
    assert "id" not in page["items"][0] and "id" not in detail["info"]
    first = read_session_info(page["items"][0], "w", "ag")
    second = read_session_info(detail["info"], "w", "ag")
    assert first == second and first.session_id == "actual-session"
    assert first.ended_reason == "completed" and detail["status"] == first.status.value == "ended"
    workspace.get_session.assert_awaited_once_with("actual-session")


@pytest.mark.parametrize("mutation", ["id-only", "workspace", "agent"])
def test_session_info_consumer_refuses_invented_or_unrelated_identity(mutation):
    from datetime import UTC, datetime
    from pydantic import ValidationError

    from primer.model.workspace_session import SessionInfo
    from tests.e2e.test_wiki_w7_t4_lifecycle import read_session_info

    now = datetime(2026, 11, 15, tzinfo=UTC)
    value = SessionInfo(session_id="s", agent_id="ag", workspace_id="w", status="running",
                        started_at=now, last_activity_at=now).model_dump(mode="json")
    if mutation == "id-only":
        value["id"] = value.pop("session_id")
    else:
        value["workspace_id" if mutation == "workspace" else "agent_id"] = "other"
    with pytest.raises((ValidationError, AssertionError)):
        read_session_info(value, "w", "ag")


def message_items(value, *, gate=None, arguments=None):
    from datetime import UTC, datetime
    from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord
    from tests.e2e.test_wiki_w7_t4_lifecycle import SPECS

    def event(kind, payload):
        return SessionMessageRecord(seq=1, kind=kind, payload=payload, created_at=datetime(2026, 11, 15, tzinfo=UTC)).model_dump(mode="json")
    answer = "RESULT:" + json.dumps(value)
    items = []
    if gate is not None:
        failed = gate in {"reject_create", "denied_create"}
        items.extend([
            event("tool_result", {"call_id": gate, "output": json.dumps(value), "error": failed}),
            Message(role="assistant", parts=[ToolCallPart(id=gate, name="system__call_tool", arguments={
                "toolset_id": "trigger", "tool_name": SPECS[gate][0], "arguments": arguments,
            })]).model_dump(mode="json"),
            Message(role="tool", parts=[ToolResultPart(id=gate, output=json.dumps(value), error=failed)]).model_dump(mode="json"),
        ])
    items.extend([
        event("assistant_token", {"text": answer}), event("done", {"stop_reason": "stop"}),
        Message(role="assistant", parts=[TextPart(text=answer)]).model_dump(mode="json"),
    ])
    return items


def valid_evidence():
    import copy
    from datetime import UTC, datetime
    from primer.bootstrap.seed import default_policy_id
    from primer.model.workspace_session import SessionInfo
    from tests.e2e import test_wiki_w7_t4_lifecycle as journey
    from tests.e2e.test_wiki_w7_t1_lifecycle import transcript_records
    from tests.e2e.test_wiki_w7_t2_lifecycle import stage_answer

    owner = {"type": "user", "id": "o", "user_id": "o", "role": "user", "display": "owner", "source": "local"}
    config = {"kind": "channel", "provider_id": "p", "channel_id": "ch", "interactive": True}
    created = {"id": "trg", "config": config, "owner": owner, "access": {"owners": ["o"]}}
    values = {"reject_create": {"rejected": True, "tool_name": "create"},
              "denied_create": {"type": "not-found"}, "create": created, "delete": {"ok": True}}
    actions = {n: {"slug": n.replace("_", "-"), "name": n, "config": config} for n in ("reject_create", "denied_create", "create")}
    actions["delete"] = {"id": "trg"}
    binding_config = {"kind": "agent_fresh_session", "workspace_id": "w", "agent_id": "ag"}
    binding = {"id": "sb", "trigger_id": "trg", "config": binding_config, "owner": owner, "reply_target": "source_thread",
               "event_matcher": {"event_type": "message.posted", "text_pattern": "^help:"}, "payload_template": "{{ event.text }}", "enabled": True}
    default = {"id": "default", "is_default": True, "access": {"owners": ["o"]}}
    trace = {"case": journey.CASE, "outcome": "passed", "source_sha": "source", "runbook_hash": "doc",
             "identities": {"owner": "o", "reader": "r", "stranger": "s"}, "approver": "a", "gates": [], "stages": {},
             "channel": "ch", "provider": "p", "workspace": "w", "external": "C", "actions": actions, "trigger": "trg",
             "binding": binding, "binding_arguments": {"config": binding_config},
             "provider_readback": {"id": "p", "config": {"app_token": "***", "bot_token": "***"}},
             "manual_fire": {"results": [{"skipped": True, "error_code": "skipped_no_match"}]},
             "standing": {"channel_id": "ch"}, "activation": {"pre_http": 409, "save_http": 200, "saved": {"id": "ch", "access": {"owners": ["o"]}}},
             "unmatched": {"event": {"text": "unmatched"}, "sessions": [], "posts": []}, "wire": [],
             "default": {"before": default, "after": copy.deepcopy(default), "directory_exists": True},
             "cleanup": [{"kind": kind, "id": id, "delete_http": 204, "get_http": 404} for kind, id in (("workspaces", "w"), ("channels", "ch"), ("channel_providers", "p"))],
             "checks": {k: True for k in ("human_activation_required", "nonmatching_message_has_no_effect", "reader_writes_refused",
                                         "binding_removed", "trigger_deleted", "standing_cleared", "owned_fixtures_deleted", "default_preserved")},
             "calls": [{"actor": "reader", "scoped_id": name, "envelope": {"ok": False, "error": {"message": "forbidden"}}}
                       for name in ("system__create_channel_binding", "system__delete_channel_binding", "trigger__fire_now")]}
    for i, (name, (operation, role, decision)) in enumerate(journey.SPECS.items()):
        items = message_items(values[name], gate=name, arguments=actions[name])
        records, answer, value = journey.read_gate(items, name)
        trace["stages"][name] = {"actor": role, "initiator": trace["identities"][role],
                                 "started": {"initiated_by": {"user_id": trace["identities"][role], "role": "user"}},
                                 "before": [created] if name == "delete" else [], "after": [created] if name == "create" else [],
                                 "items": items, "records": records, "answer": answer, "result": value}
        policy, gate = default_policy_id("trigger", operation), "gate-" + str(i)
        trace["gates"].append({"stage": name, "operation": operation, "decision": decision, "http": 202,
                               "policy_id": policy, "gate_id": gate,
                               "pending": {"policy_id": policy, "gate_id": gate, "approval_type": "required", "tool_name": operation, "arguments": actions[name]}})
    for key, text, anchor in (("inbound_session", "help: Cedar marker", "incoming"), ("manual_session", "manual: Cedar marker", "manual")):
        value = {"received": text, "answer": "CHANNEL:" + text}
        items = message_items(value);records = transcript_records(items);answer, result = stage_answer(records)
        metadata = {"trigger_id": "trg", "subscription_id": "sb", "reply_binding": {"channel_id": "ch", "anchor": "incoming"}} if key == "inbound_session" else {}
        post = {"channel": "C", "thread_ts": anchor, "text": answer}
        trace[key] = {"id": key, "row": {"initiated_by": owner, "workspace_id": "w", "binding": {"agent_id": "ag"},
                                        "metadata": metadata, "turn_status": "idle", "ended_reason": "completed"},
                      "items": items, "records": records, "answer": answer, "value": result, "posts": [post],
                      "native_view": {"info": SessionInfo(session_id=key, agent_id="ag", workspace_id="w", status="ended",
                                                         ended_reason="completed", started_at=datetime(2026, 11, 15, tzinfo=UTC),
                                                         last_activity_at=datetime(2026, 11, 15, tzinfo=UTC)).model_dump(mode="json"),
                                      "status": "ended"}}
        trace["wire"].append({"method": "chat.postMessage", "payload": post})
    trace["inbound"] = {"text": "help: Cedar marker", "thread_ts": "incoming"}
    return copy.deepcopy(trace)


@pytest.mark.parametrize(("path", "bad"), [
    (("outcome",), "running"), (("source_sha",), "other"), (("runbook_hash",), "other"),
    (("approver",), "o"), (("stages", "create", "started", "initiated_by", "role"), "admin"),
    (("gates", 0, "gate_id"), "other"), (("gates", 0, "pending", "arguments"), {"wrong": True}),
    (("binding", "owner", "user_id"), "a"), (("binding", "payload_template"), "invented"),
    (("manual_fire", "results", 0, "skipped"), False), (("activation", "pre_http"), 200),
    (("inbound_session", "posts", 0, "thread_ts"), "wrong"), (("inbound_session", "row", "workspace_id"), "other"),
    (("inbound_session", "native_view", "info", "session_id"), "other"),
    (("inbound_session", "native_view", "info", "workspace_id"), "other"),
    (("inbound_session", "native_view", "info", "agent_id"), "other"),
    (("inbound_session", "native_view", "status"), "running"),
    (("inbound_session", "row", "ended_reason"), "failed"), (("standing", "channel_id"), "other"),
    (("default", "after", "access"), {"owners": ["a"]}), (("cleanup", 0, "get_http"), 200),
    (("wire",), []), (("checks", "trigger_deleted"), False),
])
def test_required_channel_evidence_rejects_false_or_partial_acceptance(path, bad):
    from tests.e2e.test_wiki_w7_t4_lifecycle import require_evidence

    trace = valid_evidence()
    require_evidence(trace, "source", "doc")
    node = trace
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = bad
    with pytest.raises(AssertionError):
        require_evidence(trace, "source", "doc")


def test_gate_transcript_rejects_an_unexpected_error_instead_of_masking_expected_refusal():
    from tests.e2e.test_wiki_w7_t4_lifecycle import read_gate

    trace = valid_evidence()
    items = trace["stages"]["denied_create"]["items"]
    items.append({**items[0], "kind": "error", "payload": {"message": "unrelated child failure"}})
    with pytest.raises(AssertionError):
        read_gate(items, "denied_create")


@pytest.mark.parametrize("name", ["reject_create", "denied_create", "create", "delete"])
def test_gate_model_replays_the_actual_inner_result_including_exact_refusals(name):
    from tests.e2e.test_wiki_w7_t4_lifecycle import GateRule

    trace = valid_evidence()
    rule = GateRule(name, trace["actions"][name])
    request = {"messages": [{"role": "user", "content": name}]}
    assert rule.matches(request) and rule.emit_tool == "system__call_tool"
    request["messages"].append({"role": "tool", "tool_call_id": name, "content": json.dumps(trace["stages"][name]["result"])})
    assert rule.matches(request)
    assert rule.emit_tool is None and rule.emit_text == trace["stages"][name]["answer"]


@pytest.mark.parametrize("text", ["help: Cedar unique-input", "manual: Different unique-input"])
def test_reply_model_derives_its_answer_from_the_actual_received_user_text(text):
    from tests.e2e.test_wiki_w7_t4_lifecycle import ReplyRule

    rule = ReplyRule()
    assert rule.matches({"messages": [{"role": "user", "content": text}]})
    assert json.loads(rule.emit_text.removeprefix("RESULT:")) == {"received": text, "answer": "CHANNEL:" + text}
    assert not rule.matches({"messages": [{"role": "user", "content": "not an accepted operation"}]})


def workflow_steps():
    import yaml
    from tests.e2e.test_wiki_w7_t4_lifecycle import REPO

    return yaml.safe_load((REPO / ".github/workflows/e2e.yml").read_text())["jobs"]["e2e"]["steps"]


def test_channel_evidence_gate_is_required_after_the_one_original_live_producer():
    steps = workflow_steps()
    producers = [s for s in steps if "uv run pytest tests/e2e" in s.get("run", "")]
    assert len(producers) == 1 and "--junitxml=tests/.e2e/w7-t2/junit.xml" in producers[0]["run"]
    copy = next(s for s in steps if s.get("name") == "Copy live JUnit for W7 channel evidence")
    gate = next(s for s in steps if s.get("name") == "Require executed W7 channel onboarding evidence")
    upload = next(s for s in steps if s.get("name") == "Upload W7 channel onboarding transcript")
    assert steps.index(producers[0]) < steps.index(copy) < steps.index(gate) < steps.index(upload)
    assert copy["if"] == upload["if"] == "always()" and "if" not in gate and "continue-on-error" not in gate
    assert "uv run --no-sync python -" in gate["run"]
    assert upload["with"]["path"] == "tests/.e2e/w7-t4/"
    assert "w7-t4-channel-onboarding" in upload["with"]["name"]


def test_actual_copy_and_gate_in_a_clean_shell_fail_closed_without_real_named_evidence(tmp_path):
    import copy
    import hashlib
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    from xml.etree import ElementTree as ET
    from tests.e2e import test_wiki_w7_t4_lifecycle as journey

    steps = workflow_steps()
    command = next(s["run"] for s in steps if s.get("name") == "Require executed W7 channel onboarding evidence")
    copier = next(s["run"] for s in steps if s.get("name") == "Copy live JUnit for W7 channel evidence")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(journey.REPO / name, tmp_path / name)
    (tmp_path / "runtime").mkdir()
    shutil.copyfile(journey.REPO / "runtime/pyproject.toml", tmp_path / "runtime/pyproject.toml")
    (tmp_path / "primer").symlink_to(journey.REPO / "primer", target_is_directory=True)
    (tmp_path / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (tmp_path / "tests/e2e").mkdir(parents=True)
    for name in ("__init__.py", "test_wiki_w7_t1_lifecycle.py", "test_wiki_w7_t2_lifecycle.py", "test_wiki_w7_t4_lifecycle.py"):
        shutil.copyfile(journey.REPO / "tests/e2e" / name, tmp_path / "tests/e2e" / name)
    (tmp_path / "tests/_support").symlink_to(journey.REPO / "tests/_support", target_is_directory=True)
    page = tmp_path / "docs/wiki" / (journey.PAGE + ".md")
    page.parent.mkdir(parents=True)
    page.write_bytes((journey.REPO / "docs/wiki" / (journey.PAGE + ".md")).read_bytes())
    trace = valid_evidence()
    trace["runbook_hash"] = hashlib.sha256(page.read_bytes()).hexdigest()
    evidence = tmp_path / "tests/.e2e/w7-t4/trace.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps(trace))
    producer = tmp_path / "tests/.e2e/w7-t2/junit.xml"
    producer.parent.mkdir(parents=True)
    root = ET.Element("testsuites");suite = ET.SubElement(root, "testsuite")
    ET.SubElement(suite, "testcase", classname="tests.e2e.test_wiki_w7_t4_lifecycle", name=journey.CASE)
    good = ET.tostring(root, encoding="unicode");producer.write_text(good)
    bare = tmp_path / "bare"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True, timeout=30)
    uv = shutil.which("uv");assert uv
    env = {"PATH": os.pathsep.join((str(bare / "bin"), str(Path(uv).parent), "/usr/bin", "/bin")),
           "HOME": os.environ["HOME"], "GITHUB_SHA": "source", "PYTHONNOUSERSITE": "1",
           "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": str(tmp_path / "cache")}
    assert not {"UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONPATH"} & set(env)
    def shell(script, environment=env):
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                              cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=45)
    assert shell(copier).returncode == 0
    junit = evidence.with_name("junit.xml")
    assert junit.read_bytes() == producer.read_bytes()
    producer.unlink()
    assert shell(copier).returncode != 0 and not junit.exists()
    producer.write_text(good);assert shell(copier).returncode == 0
    bad_launcher = shell(command.replace("uv run --no-sync python -", "python3 -", 1))
    assert bad_launcher.returncode != 0 and "ModuleNotFoundError" in bad_launcher.stderr
    good_run = shell(command)
    assert good_run.returncode == 0, good_run.stderr
    evidence.unlink();assert shell(command).returncode != 0
    evidence.write_text(json.dumps(trace))
    assert shell(command, {**env, "GITHUB_SHA": "wrong-source"}).returncode != 0
    for damage in ("missing", "duplicate", "wrong-name", "failure", "error", "skipped", "malformed", "no-report"):
        doc = ET.fromstring(good);case = doc.find(".//testcase");suite = doc.find("testsuite")
        if damage == "missing":suite.remove(case)
        elif damage == "duplicate":suite.append(copy.deepcopy(case))
        elif damage == "wrong-name":case.set("name", "another_case")
        elif damage in {"failure", "error", "skipped"}:ET.SubElement(case, damage)
        junit.write_text("<broken" if damage == "malformed" else ET.tostring(doc, encoding="unicode"))
        if damage == "no-report":junit.unlink()
        assert shell(command).returncode != 0, damage
    junit.write_text(good)
    assert shell(command).returncode == 0
