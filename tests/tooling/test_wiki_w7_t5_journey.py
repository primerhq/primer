"""Safe T5 model/isolation/evidence probes; actual lifecycle is CI-owned."""

import copy
from datetime import UTC, datetime
import json

import pytest

from tests.e2e import test_wiki_w7_t5_lifecycle as journey


def message_items(value, *, gate=None, arguments=None):
    from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord

    def event(kind, payload):
        return SessionMessageRecord(seq=1, kind=kind, payload=payload, created_at=datetime(2026, 11, 15, tzinfo=UTC)).model_dump(mode="json")
    answer = "RESULT:" + json.dumps(value);items = []
    if gate is not None:
        failed = gate in {"reject_create", "denied_update"}
        items.extend([
            event("tool_result", {"call_id": gate, "output": json.dumps(value), "error": failed}),
            Message(role="assistant", parts=[ToolCallPart(id=gate, name="system__call_tool", arguments={
                "toolset_id": "trigger", "tool_name": journey.SPECS[gate][0], "arguments": arguments,
            })]).model_dump(mode="json"),
            Message(role="tool", parts=[ToolResultPart(id=gate, output=json.dumps(value), error=failed)]).model_dump(mode="json"),
        ])
    items.extend([event("assistant_token", {"text": answer}), event("done", {"stop_reason": "stop"}),
                  Message(role="assistant", parts=[TextPart(text=answer)]).model_dump(mode="json")])
    return items


def valid_evidence():
    from primer.bootstrap.seed import default_policy_id
    from primer.model.workspace_session import SessionInfo
    from primer.trigger.owner import WEBHOOK_TOKEN_MASK

    owner = {"type": "user", "id": "o", "user_id": "o", "role": "user", "display": "owner", "source": "local"}
    config = {"kind": "delayed", "fire_at": "2099-01-01T09:00:00Z"}
    created = {"id": "trg", "config": config, "enabled": True, "owner": owner, "access": {"owners": ["o"]}}
    webhook = {"id": "web", "config": {"kind": "webhook", "token": "a" * 32}, "enabled": True,
               "owner": owner, "access": {"owners": ["o"]}}
    values = {"reject_create": {"rejected": True, "tool_name": "create"}, "denied_update": {"type": "forbidden"},
              "create": created, "disable": {**created, "enabled": False, "next_fire_at": None},
              "webhook": webhook, "delete": {"ok": True}, "delete_webhook": {"ok": True}}
    actions = {n: {"slug": n.replace("_", "-"), "name": n, "config": config} for n in ("reject_create", "create")}
    actions.update(denied_update={"id": "trg", "enabled": False}, disable={"id": "trg", "enabled": False},
                   webhook={"slug": "webhook", "name": "webhook", "config": {"kind": "webhook"}},
                   delete={"id": "trg"}, delete_webhook={"id": "web"})
    sub = {"id": "sb", "trigger_id": "trg", "config": {"kind": "agent_fresh_session", "workspace_id": "w", "agent_id": "ag"},
           "owner": owner, "payload_template": "TRIGGER:sample {{ fire_id }} {{ trigger_id }}", "enabled": True, "parallelism": "queue"}
    default = {"id": "default", "is_default": True, "access": {"owners": ["o"]}}
    trace = {"case": journey.CASE, "outcome": "passed", "source_sha": "source", "runbook_hash": "doc",
             "wiki": {"path": journey.PAGE, "body": (journey.REPO / "docs/wiki" / (journey.PAGE + ".md")).read_text().split("---", 2)[2].strip()},
             "identities": {"owner": "o", "reader": "r", "stranger": "s"}, "approver": "a", "stages": {}, "gates": [],
             "actions": actions, "trigger": "trg", "workspace": "w", "subscription": sub, "payload_template": sub["payload_template"],
             "subscription_created": {**sub, "enabled": False},
             "disabled_subscription_fire": {"skipped": False, "fire_id": "empty", "results": []},
             "disabled_trigger_fire": {"skipped": True, "fire_id": None, "results": []},
             "reader_no_effect": {"before": created, "after": copy.deepcopy(created), "sub_before": sub, "sub_after": copy.deepcopy(sub)},
             "hidden": {"ok": False, "error": {"message": "trigger_not_found"}},
             "subscription_deleted": {"arguments": {"trigger_id": "trg", "subscription_id": "sb"}, "list": [],
                                      "missing": {"ok": False, "error": {"message": "subscription_not_found"}}},
             "remaining_triggers": [], "cleanup": {"workspace": "w", "delete_http": 204, "get_http": 404},
             "default": {"before": default, "after": copy.deepcopy(default), "directory_exists": True},
             "checks": {k: True for k in ("rejection_no_effect", "approved_reader_refused", "disabled_subscription_no_delivery",
                 "reader_writes_no_effect", "actual_worker_answer", "disabled_trigger_no_delivery", "subscription_removed",
                 "webhook_token_visibility", "owned_triggers_removed", "workspace_removed", "default_preserved")}}
    for i, (name, (operation, actor, decision)) in enumerate(journey.SPECS.items()):
        items = message_items(values[name], gate=name, arguments=actions[name])
        records, answer, value = journey.read_gate(items, name)
        trace["stages"][name] = {"actor": actor, "initiator": trace["identities"][actor],
            "started": {"initiated_by": {"user_id": trace["identities"][actor], "role": "user"}}, "before": [],
            "after": [values[name]] if name in {"create", "webhook"} else [],
            "items": items, "records": records, "answer": answer, "result": value}
        policy, gate = default_policy_id("trigger", operation), "gate-" + str(i)
        trace["gates"].append({"stage": name, "operation": operation, "decision": decision, "http": 202, "policy_id": policy, "gate_id": gate,
            "pending": {"policy_id": policy, "gate_id": gate, "approval_type": "required", "tool_name": operation, "arguments": actions[name]}})
    expected = "TRIGGER:sample fire-real trg";value = {"received": expected, "answer": "PROVED:" + expected}
    items = message_items(value);records = journey.transcript_records(items);answer, result = journey.stage_answer(records)
    now = datetime(2026, 11, 15, tzinfo=UTC)
    info = SessionInfo(session_id="sess", workspace_id="w", agent_id="ag", status="ended", ended_reason="completed",
                       started_at=now, last_activity_at=now).model_dump(mode="json")
    trace["fire"] = {"skipped": False, "fire_id": "fire-real", "results": [{"subscription_id": "sb", "artefact_id": "sess", "ok": True, "skipped": False, "error_code": None}]}
    trace["actual_run"] = {"id": "sess", "row": {"id": "sess", "workspace_id": "w", "binding": {"agent_id": "ag"}, "initiated_by": owner,
        "metadata": {"trigger_id": "trg", "subscription_id": "sb", "fire_id": "fire-real"}, "turn_status": "idle", "ended_reason": "completed", "initial_instructions": expected},
        "items": items, "records": records, "answer": answer, "value": result, "native_view": {"info": info, "status": "ended"}, "listed": [info],
        "subscription_outcome": {**sub, "last_fired_at": now.isoformat(), "last_fire_error": None}}
    reader = copy.deepcopy(webhook);reader["config"]["token"] = WEBHOOK_TOKEN_MASK;reader["owner"] = {"display": "owner", "role": "user"}
    trace["webhook_views"] = {"owner": webhook, "admin": copy.deepcopy(webhook), "reader": reader, "reader_list": [reader]}
    trace["calls"] = [{"actor": "reader", "scoped_id": "trigger__" + name, "envelope": {"ok": False, "error": {"message": "forbidden"}}}
        for name in ("create_subscription", "update_subscription", "delete_subscription", "fire_now")]
    trace["calls"] += [{"actor": "owner", "scoped_id": "workspaces__list_workspace_sessions", "envelope": {"ok": True, "output": json.dumps({"items": rows})}}
        for rows in ([], [], [], [info], [info])]
    return copy.deepcopy(trace)


@pytest.mark.parametrize(("path", "bad"), [
    (("outcome",), "running"), (("source_sha",), "other"), (("runbook_hash",), "other"), (("wiki", "body"), "invented"),
    (("approver",), "o"), (("stages", "create", "started", "initiated_by", "role"), "admin"),
    (("gates", 0, "gate_id"), "other"), (("gates", 0, "pending", "arguments"), {}),
    (("subscription", "owner", "user_id"), "a"), (("subscription", "enabled"), False),
    (("disabled_subscription_fire", "results"), [{"ok": True}]), (("disabled_trigger_fire", "skipped"), False),
    (("reader_no_effect", "after", "enabled"), False), (("fire", "results", 0, "artefact_id"), "other"),
    (("actual_run", "row", "initiated_by", "role"), "admin"), (("actual_run", "row", "metadata", "fire_id"), "other"),
    (("actual_run", "native_view", "info", "session_id"), "other"), (("actual_run", "native_view", "status"), "running"),
    (("actual_run", "row", "ended_reason"), "failed"), (("actual_run", "subscription_outcome", "last_fire_error"), "failed"),
    (("webhook_views", "reader", "config", "token"), "a" * 32), (("webhook_views", "admin", "config", "token"), "masked"),
    (("remaining_triggers",), [{"id": "trg"}]), (("cleanup", "get_http"), 200),
    (("default", "after", "access"), {"owners": ["a"]}), (("checks", "subscription_removed"), False),
])
def test_required_trigger_evidence_rejects_partial_or_false_acceptance(path, bad):
    trace = valid_evidence();journey.require_evidence(trace, "source", "doc")
    node = trace
    for key in path[:-1]:node = node[key]
    node[path[-1]] = bad
    with pytest.raises(AssertionError):journey.require_evidence(trace, "source", "doc")


@pytest.mark.parametrize("name", journey.SPECS)
def test_gate_model_replays_exact_real_result_and_refusal(name):
    trace = valid_evidence();rule = journey.GateRule(name, trace["actions"][name])
    request = {"messages": [{"role": "user", "content": name}]}
    assert rule.matches(request) and rule.emit_tool == "system__call_tool"
    request["messages"].append({"role": "tool", "tool_call_id": name, "content": json.dumps(trace["stages"][name]["result"])})
    assert rule.matches(request) and rule.emit_tool is None and rule.emit_text == trace["stages"][name]["answer"]


def test_unexpected_child_error_or_missing_terminal_cannot_be_accepted_as_refusal():
    trace = valid_evidence();items = trace["stages"]["denied_update"]["items"]
    items.append({**items[0], "kind": "error", "payload": {"message": "unrelated child failure"}})
    with pytest.raises(AssertionError):journey.read_gate(items, "denied_update")
    items = [r for r in trace["stages"]["create"]["items"] if r.get("kind") != "done"]
    with pytest.raises(AssertionError):journey.read_gate(items, "create")


def test_reply_model_derives_answer_from_the_actual_rendered_fire_instruction():
    rule = journey.ReplyRule();text = "TRIGGER:unique fire-123 tr-42"
    assert rule.matches({"messages": [{"role": "user", "content": text}]})
    assert json.loads(rule.emit_text.removeprefix("RESULT:")) == {"received": text, "answer": "PROVED:" + text}
    assert not rule.matches({"messages": [{"role": "user", "content": "unrelated"}]})


def test_private_config_has_auth_worker_sqlite_complete_wiki_and_scrubbed_environment(tmp_path, monkeypatch):
    from primer.api.config import AppConfig
    from primer.knowledge.wiki.pages import load_source_pages

    monkeypatch.setenv("PRIMER_TEST_POSTGRES_URL", "never-inherit")
    monkeypatch.setenv("PRIMER_WIKI_DIR", "never-inherit")
    config_path, env = journey.prepare_config(tmp_path, 50124)
    config = AppConfig.model_validate(json.loads(config_path.read_text()))
    assert config.auth.enabled and not config.auto_bootstrap and config.runtime_mode.value == "api+worker"
    assert config.host == "127.0.0.1" and config.port == 50124 and config.db.provider.value == "sqlite"
    assert str(config.db.config.path) == str(tmp_path / "data.sqlite") and not (tmp_path / "data.sqlite").exists()
    assert "PRIMER_TEST_POSTGRES_URL" not in env and env["PRIMER_WIKI_DIR"] == str(tmp_path / "wiki")
    assert set(load_source_pages(tmp_path / "wiki")) == {"index", "runbooks", journey.PAGE}
    assert list((tmp_path / "legacy-empty").iterdir()) == []


def workflow_steps():
    import yaml
    return yaml.safe_load((journey.REPO / ".github/workflows/e2e.yml").read_text())["jobs"]["e2e"]["steps"]


def test_trigger_evidence_gate_is_required_after_the_one_original_live_producer():
    steps = workflow_steps()
    producers = [s for s in steps if "uv run pytest tests/e2e" in s.get("run", "")]
    assert len(producers) == 1 and "--junitxml=tests/.e2e/w7-t2/junit.xml" in producers[0]["run"]
    copy = next(s for s in steps if s.get("name") == "Copy live JUnit for W7 trigger evidence")
    gate = next(s for s in steps if s.get("name") == "Require executed W7 trigger and subscription evidence")
    upload = next(s for s in steps if s.get("name") == "Upload W7 trigger and subscription transcript")
    assert steps.index(producers[0]) < steps.index(copy) < steps.index(gate) < steps.index(upload)
    assert copy["if"] == upload["if"] == "always()" and "if" not in gate and "continue-on-error" not in gate
    assert "uv run --no-sync python -" in gate["run"]
    assert upload["with"]["path"] == "tests/.e2e/w7-t5/"
    assert "w7-t5-trigger-subscription" in upload["with"]["name"]


def test_actual_copy_and_gate_in_a_clean_shell_fail_closed_without_real_named_evidence(tmp_path):
    import copy
    import hashlib
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    from xml.etree import ElementTree as ET
    from tests.e2e import test_wiki_w7_t5_lifecycle as journey

    steps = workflow_steps()
    command = next(s["run"] for s in steps if s.get("name") == "Require executed W7 trigger and subscription evidence")
    copier = next(s["run"] for s in steps if s.get("name") == "Copy live JUnit for W7 trigger evidence")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(journey.REPO / name, tmp_path / name)
    (tmp_path / "runtime").mkdir()
    shutil.copyfile(journey.REPO / "runtime/pyproject.toml", tmp_path / "runtime/pyproject.toml")
    (tmp_path / "primer").symlink_to(journey.REPO / "primer", target_is_directory=True)
    (tmp_path / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (tmp_path / "tests/e2e").mkdir(parents=True)
    for name in ("__init__.py", "test_wiki_w7_t1_lifecycle.py", "test_wiki_w7_t2_lifecycle.py", "test_wiki_w7_t4_lifecycle.py", "test_wiki_w7_t5_lifecycle.py"):
        shutil.copyfile(journey.REPO / "tests/e2e" / name, tmp_path / "tests/e2e" / name)
    (tmp_path / "tests/_support").symlink_to(journey.REPO / "tests/_support", target_is_directory=True)
    page = tmp_path / "docs/wiki" / (journey.PAGE + ".md")
    page.parent.mkdir(parents=True)
    page.write_bytes((journey.REPO / "docs/wiki" / (journey.PAGE + ".md")).read_bytes())
    trace = valid_evidence()
    trace["runbook_hash"] = hashlib.sha256(page.read_bytes()).hexdigest()
    evidence = tmp_path / "tests/.e2e/w7-t5/trace.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps(trace))
    producer = tmp_path / "tests/.e2e/w7-t2/junit.xml"
    producer.parent.mkdir(parents=True)
    root = ET.Element("testsuites");suite = ET.SubElement(root, "testsuite")
    ET.SubElement(suite, "testcase", classname="tests.e2e.test_wiki_w7_t5_lifecycle", name=journey.CASE)
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
