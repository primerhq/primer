"""Safe fixture/model/evidence probes; no server or live lifecycle runs here."""

import copy
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.e2e import test_wiki_w7_t6_lifecycle as journey
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401


def message_items(name, arguments, value):
    from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord

    def event(kind, payload):
        return SessionMessageRecord(seq=1, kind=kind, payload=payload, created_at=datetime(2026, 11, 15, tzinfo=UTC)).model_dump(mode="json")
    failed = journey.SPECS[name][1] == "rejected"
    output = json.dumps(value);answer = "RESULT:" + output
    return [event("tool_result", {"call_id": name, "output": output, "error": failed}),
        Message(role="assistant", parts=[ToolCallPart(id=name, name="system__call_tool", arguments={
            "toolset_id": "harness", "tool_name": journey.SPECS[name][0], "arguments": arguments})]).model_dump(mode="json"),
        Message(role="tool", parts=[ToolResultPart(id=name, output=output, error=failed)]).model_dump(mode="json"),
        event("assistant_token", {"text": answer}), event("done", {"stop_reason": "stop"}),
        Message(role="assistant", parts=[TextPart(text=answer)]).model_dump(mode="json")]


def valid_evidence():
    from primer.bootstrap.seed import default_policy_id
    from primer.model.agent import Agent
    from primer.model.graph import Graph
    from primer.model.providers.artifact import DEFAULT_ARTIFACT_PROVIDER_ID

    original = {"provider_id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "db"}
    provider = {"id": "store", "provider": "filesystem", "config": {"root": "/private/artifacts"}}
    default_provider = {"id": DEFAULT_ARTIFACT_PROVIDER_ID, "provider": "db", "config": {}}
    default = {"id": "default", "is_default": True, "access": {"owners": ["runner"]}}
    base = {"id": "hns", "slug": "sample", "status": "ready", "pending_operation": None, "last_operation_error": None,
            "available_commit": "a" * 40, "git_url": "file:///private/bundle.git", "overrides_schema": {"type": "object"}, "access": {"owners": ["runner"]}}
    actions = {name: {"id": "hns", **({"overrides": {"profile_id": "profile", "description": "v2" if name == "sync" else "v1"}} if operation != "uninstall" else {})}
               for name, (operation, _) in journey.SPECS.items()}
    trace = {"case": journey.CASE, "source_sha": "source", "runbook_hashes": dict.fromkeys(journey.PAGES, "doc"), "outcome": "passed",
        "wiki": {p: {"path": p, "body": (journey.REPO / "docs/wiki" / (p + ".md")).read_text().split("---", 2)[2].strip()} for p in journey.PAGES},
        "identities": {"runner": "runner", "reader": "reader", "stranger": "stranger"}, "approver": "approver", "slug": "sample",
        "git": {"url": base["git_url"], "commit": "a" * 40}, "registered": {**base, "status": "draft"},
        "hidden": {"ok": False, "error": {"message": "not-found"}}, "fetch_queued": {**base, "pending_operation": "fetch"}, "fetched": base,
        "reader_no_effect": {"before": base, "after": copy.deepcopy(base)},
        "no_schema": {"ok": False, "error": {"message": "overrides-schema-missing"}},
        "invalid_overrides": {"ok": False, "error": {"message": "overrides-invalid"}}, "rejection_missing": {"agent": 404, "graph": 404},
        "actions": actions, "stages": {}, "gates": [], "rejected_after": base,
        "uninstalled": {"harness": 404, "agent": 404, "graph": 404, "list": {"items": []}},
        "calls": [{"actor": "reader", "scoped_id": scoped, "envelope": {"ok": scoped == "harness__get"}} for scoped in (
            "harness__register", "harness__get", "harness__fetch", "system__create_artifact_storage_provider", "system__set_active_artifact_store", "system__get_active_artifact_store")],
        "default": {"before": default, "after": copy.deepcopy(default), "directory_exists": True},
    }
    for number, (name, (operation, decision)) in enumerate(journey.SPECS.items()):
        value = {"rejected": True, "tool_name": operation} if decision == "rejected" else {**base, "pending_operation": operation}
        items = message_items(name, actions[name], value);records, answer, result, _ = journey.read_gate(items, name)
        row = {"initiated_by": {"user_id": "runner", "role": "admin"}, "turn_status": "idle", "ended_reason": "completed"}
        trace["stages"][name] = {"started": row, "row": row, "before": base, "items": items, "records": records, "answer": answer, "result": result}
        policy, gate = default_policy_id("harness", operation), "gate-" + str(number)
        trace["gates"].append({"stage": name, "operation": operation, "decision": decision, "http": 202, "policy_id": policy, "gate_id": gate,
            "reader_http": 403, "reader_body": {"detail": {"error": "approver_mismatch"}}, "pending": {"tool_name": operation, "policy_id": policy,
            "gate_id": gate, "approval_type": "required", "arguments": actions[name], "approvers": {"kind": "roles", "roles": [], "users": []}}})
    for version, state, action in (("v1", "installed", "install"), ("v2", "synced", "sync")):
        values = actions[action]["overrides"]
        trace[state] = {**base, "status": "installed", "resolved_commit": "a" * 40, "overrides": values}
        agent = Agent(id="sample__assistant", description=values["description"], harness_id="hns", model={"profile_id": "profile"})
        graph = Graph(id="sample__workflow", description=values["description"], harness_id="hns", nodes=[
            {"kind": "begin", "id": "begin"}, {"kind": "agent", "id": "answer", "agent_id": agent.id}, {"kind": "end", "id": "end"}], edges=[
            {"kind": "static", "from_node": "begin", "to_node": "answer"}, {"kind": "static", "from_node": "answer", "to_node": "end"}])
        trace["entities_" + version] = {"agent": agent.model_dump(mode="json"), "graph": graph.model_dump(mode="json")}
    versions, contents = [{"id": "v1"}, {"id": "v2"}], ["DB bytes", "FS bytes"]
    active = {"provider_id": "store", "provider": "filesystem"}
    def digest(value):
        import hashlib
        return hashlib.sha256(value.encode()).hexdigest()
    trace["artifact"] = {"original": original, "restored": original, "default_before": default_provider, "default_after": copy.deepcopy(default_provider),
        "created": provider, "probe": {"ok": True}, "active": active, "missing_selection": {"ok": False}, "versions": versions, "contents": contents,
        "reads": [{"version": versions[i], "http": 200, "content": contents[i], "sha256": digest(contents[i])} for i in (1, 0)],
        "stored_sha256": digest(contents[1]), "old_on_fs": {"http": 200, "content": contents[0], "active": active},
        "blocked_active": {"extensions": {"error": "cascade_blocked"}},
        "blocked_held": {"extensions": {"error": "artifact_provider_holds_artifacts", "count": 1}},
        "blocked_move": {"extensions": {"error": "artifact_provider_holds_artifacts", "count": 1}},
        "moved_root_exists": False, "service_delete": 204, "blobs_after": [], "provider_deleted": {"deleted": True, "id": "store"}, "provider_missing": {"ok": False}}
    def call(scoped, args, value):
        trace["calls"].append({"actor": "runner", "scoped_id": scoped, "arguments": args, "envelope": {"ok": True, "output": json.dumps(value)}})
    for page in journey.PAGES:call("system__wiki_read", {"path": page}, trace["wiki"][page])
    call("harness__get", {"id": "hns"}, trace["fetched"])
    for version, state in (("v1", "installed"), ("v2", "synced")):
        call("harness__get", {"id": "hns"}, trace[state])
        for kind in ("agent", "graph"):
            value = trace["entities_" + version][kind];call("system__get_" + kind, {"id": value["id"]}, value)
    call("system__create_artifact_storage_provider", {"entity": provider}, provider)
    call("system__test_artifact_storage_provider", {"id": "store"}, {"ok": True})
    call("system__set_active_artifact_store", {"provider_id": "store"}, active)
    call("system__set_active_artifact_store", {"provider_id": DEFAULT_ARTIFACT_PROVIDER_ID}, original)
    call("system__get_active_artifact_store", {}, original)
    call("system__get_artifact_storage_provider", {"id": DEFAULT_ARTIFACT_PROVIDER_ID}, default_provider)
    call("system__delete_artifact_storage_provider", {"id": "store"}, {"deleted": True, "id": "store"})
    return copy.deepcopy(trace)


@pytest.mark.parametrize(("path", "bad"), [
    (("outcome",), "running"), (("source_sha",), "other"), (("runbook_hashes",), {}), (("wiki", journey.PAGES[1], "body"), "invented"),
    (("approver",), "runner"), (("registered", "access", "owners"), ["approver"]), (("hidden", "ok"), True),
    (("reader_no_effect", "after", "pending_operation"), "fetch"), (("no_schema", "error", "message"), "unrelated"),
    (("invalid_overrides", "ok"), True), (("rejection_missing", "agent"), 200),
    (("fetched", "available_commit"), "b" * 40), (("fetched", "pending_operation"), "fetch"),
    (("gates", 0, "gate_id"), "other"), (("gates", 0, "pending", "arguments"), {}), (("gates", 1, "reader_http"), 202),
    (("gates", 1, "pending", "approvers", "roles"), ["user"]), (("stages", "install", "row", "initiated_by", "user_id"), "approver"),
    (("stages", "install", "row", "ended_reason"), "failed"), (("synced", "pending_operation"), "sync"),
    (("entities_v2", "agent", "description"), "old"), (("entities_v1", "graph", "nodes", 1, "agent_id"), "other"),
    (("entities_v2", "agent", "harness_id"), "another"), (("uninstalled", "agent"), 200),
    (("uninstalled", "list", "items"), [{"id": "hns"}]), (("calls", 0, "envelope", "ok"), True),
    (("artifact", "restored", "provider_id"), "store"), (("artifact", "probe", "ok"), False),
    (("artifact", "old_on_fs", "content"), "wrong"), (("artifact", "reads", 0, "content"), "wrong"),
    (("artifact", "stored_sha256"), "wrong"), (("artifact", "blocked_held", "extensions", "count"), 0),
    (("artifact", "moved_root_exists"), True), (("artifact", "blobs_after"), ["leftover"]),
    (("artifact", "default_after", "provider"), "filesystem"), (("default", "after", "access"), {"owners": ["approver"]}),
    (("default", "directory_exists"), False),
    (("calls", 6, "envelope", "output"), "{}"), (("calls", 13, "arguments", "id"), "wrong"),
])
def test_required_evidence_rejects_partial_wrong_identity_or_false_effects(path, bad):
    trace = valid_evidence();journey.require_evidence(trace, "source", dict.fromkeys(journey.PAGES, "doc"))
    node = trace
    for key in path[:-1]:node = node[key]
    node[path[-1]] = bad
    with pytest.raises(AssertionError):journey.require_evidence(trace, "source", dict.fromkeys(journey.PAGES, "doc"))


@pytest.mark.parametrize("name", journey.SPECS)
def test_gate_model_uses_exact_native_operation_and_actual_result(name):
    trace = valid_evidence();rule = journey.GateRule(name, trace["actions"][name])
    request = {"messages": [{"role": "user", "content": name}]}
    assert rule.matches(request) and rule.emit_tool == "system__call_tool"
    assert rule.emit_args == {"toolset_id": "harness", "tool_name": journey.SPECS[name][0], "arguments": trace["actions"][name]}
    request["messages"].append({"role": "tool", "tool_call_id": name, "content": json.dumps(trace["stages"][name]["result"])})
    assert rule.matches(request) and rule.emit_tool is None and rule.emit_text == trace["stages"][name]["answer"]


def test_unexpected_error_raw_failure_or_missing_terminal_is_never_acceptance():
    items = valid_evidence()["stages"]["install"]["items"]
    with pytest.raises(AssertionError):journey.read_gate([r for r in items if r.get("kind") != "done"], "install")
    items[0]["payload"]["error"] = True
    with pytest.raises(AssertionError):journey.read_gate(items, "install")
    items = valid_evidence()["stages"]["reject_install"]["items"]
    items.append({**items[0], "kind": "error", "payload": {"message": "unrelated child failure"}})
    with pytest.raises(AssertionError):journey.read_gate(items, "reject_install")


@pytest.mark.asyncio
async def test_real_bundle_clone_metadata_render_rewrite_and_runnability(tmp_path, monkeypatch):
    from primer.harness.git import clone_at_ref, fetch_harness_metadata
    from primer.harness.service import build_rendered_entries
    from primer.harness.template import render_bundle
    from primer.model.graph import Graph
    from primer.model.harness import FILE_GIT_URLS_ENV, validate_git_url

    url, commit = journey.make_git_bundle(tmp_path)
    monkeypatch.delenv(FILE_GIT_URLS_ENV, raising=False)
    with pytest.raises(ValueError):validate_git_url(url)
    monkeypatch.setenv(FILE_GIT_URLS_ENV, "1")
    metadata, schema, _, resolved = await fetch_harness_metadata(git_url=url, ref="main", subpath=None, token=None)
    assert metadata["kind"] == "Harness" and resolved == commit and schema["additionalProperties"] is False
    checkout = tmp_path / "checkout";await clone_at_ref(url, token=None, ref="main", dest=str(checkout))
    for description in ("original", "changed"):
        rendered = await render_bundle(checkout_dir=str(checkout), subpath=None, overrides={"profile_id": "profile", "description": description}, harness_ctx={"slug": "sample"})
        entries, errors = build_rendered_entries(rendered, slug="sample");assert not errors
        values = {e.kind: e for e in entries};assert set(values) == {"agent", "graph"}
        assert values["agent"].resolved_id == "sample__assistant" and values["graph"].resolved_id == "sample__workflow"
        assert values["agent"].rendered_payload["description"] == values["graph"].rendered_payload["description"] == description
        graph = Graph.model_validate(values["graph"].rendered_payload);graph.assert_runnable()
        assert [n.agent_id for n in graph.nodes if n.kind == "agent"] == [values["agent"].resolved_id]


def test_private_child_config_and_mocked_launcher_preserve_parent_and_gate_boundaries(tmp_path, monkeypatch):
    from primer.api.config import AppConfig
    from primer.knowledge.wiki.pages import load_source_pages
    from primer.model.harness import FILE_GIT_URLS_ENV
    from tests.pg_gate import CANONICAL_ENV, DEPRECATED_ALIASES, REQUIRE_ENV

    gates = (CANONICAL_ENV, *DEPRECATED_ALIASES, REQUIRE_ENV)
    for name in (*gates, "PRIMER_WIKI_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT"):monkeypatch.setenv(name, "must-not-inherit")
    monkeypatch.delenv(FILE_GIT_URLS_ENV, raising=False)
    captured = {}
    def popen(argv, **kwargs):captured.update(argv=argv, **kwargs);return "child"
    monkeypatch.setattr(journey.subprocess, "Popen", popen)
    assert journey.start_server(tmp_path, 50124, "log") == "child"
    env = captured["env"];config = AppConfig.model_validate(json.loads((tmp_path / "config.json").read_text()))
    assert config.auth.enabled and not config.auto_bootstrap and config.runtime_mode.value == "api+worker"
    assert config.host == "127.0.0.1" and config.port == 50124 and config.db.provider.value == "sqlite"
    assert str(config.db.config.path) == str(tmp_path / "data.sqlite") and not (tmp_path / "data.sqlite").exists()
    assert env[FILE_GIT_URLS_ENV] == "1" and not set(gates) & env.keys()
    assert set(load_source_pages(tmp_path / "wiki")) == {"index", "runbooks", *journey.PAGES}
    assert captured["argv"] == [sys.executable, "-m", "primer", "api", "--config", str(tmp_path / "config.json")]
    assert captured["cwd"] == journey.REPO and captured["stderr"] == subprocess.STDOUT
    assert captured["start_new_session"] is True
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT"} & env.keys()
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    import os
    assert FILE_GIT_URLS_ENV not in os.environ and all(os.environ[n] == "must-not-inherit" for n in gates)


def test_temporary_git_fixture_cannot_follow_an_inherited_repository(tmp_path, monkeypatch):
    unrelated = tmp_path / "unrelated.git"
    monkeypatch.setenv("GIT_DIR", str(unrelated))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "unrelated-work"))
    url, commit = journey.make_git_bundle(tmp_path)
    assert url == (tmp_path / "bundle.git").as_uri() and len(commit) == 40
    assert not unrelated.exists() and not (tmp_path / "unrelated-work").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_process_group_cleanup_is_bounded_and_kills_only_the_private_child(monkeypatch, timeout):
    import signal

    signals, waits = [], []
    class Process:
        pid = 123456
        def wait(self, *, timeout):
            waits.append(timeout)
            if len(waits) == 1 and timeout == 10 and test_timeout:raise subprocess.TimeoutExpired("private child", 10)
            return 0
    test_timeout = timeout
    monkeypatch.setattr(journey.os, "killpg", lambda pid, value: signals.append((pid, value)))
    await journey.stop_server(Process())
    assert waits == ([10, 5] if timeout else [10])
    assert signals[0] == (123456, signal.SIGTERM) and signals[-1] == (123456, signal.SIGKILL)
    assert all(pid == 123456 for pid, _ in signals)


def workflow_steps():
    import yaml
    return yaml.safe_load((journey.REPO / ".github/workflows/e2e.yml").read_text())["jobs"]["e2e"]["steps"]


def test_evidence_gate_is_required_after_the_one_unmodified_live_producer():
    steps = workflow_steps();producers = [s for s in steps if "uv run pytest tests/e2e" in s.get("run", "")]
    assert len(producers) == 1 and "--junitxml=tests/.e2e/w7-t2/junit.xml" in producers[0]["run"]
    copier = next(s for s in steps if s.get("name") == "Copy live JUnit for W7 harness and artifact evidence")
    gate = next(s for s in steps if s.get("name") == "Require executed W7 harness and artifact evidence")
    upload = next(s for s in steps if s.get("name") == "Upload W7 harness and artifact transcript")
    assert steps.index(producers[0]) < steps.index(copier) < steps.index(gate) < steps.index(upload)
    assert copier["if"] == upload["if"] == "always()" and "if" not in gate and "continue-on-error" not in gate
    assert "uv run --no-sync python -" in gate["run"] and upload["with"]["path"] == "tests/.e2e/w7-t6/"


def test_actual_copy_and_gate_in_a_clean_shell_fail_closed_without_real_named_evidence(tmp_path):
    import copy
    import hashlib
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    from xml.etree import ElementTree as ET
    from tests.e2e import test_wiki_w7_t6_lifecycle as journey

    steps = workflow_steps()
    command = next(s["run"] for s in steps if s.get("name") == "Require executed W7 harness and artifact evidence")
    copier = next(s["run"] for s in steps if s.get("name") == "Copy live JUnit for W7 harness and artifact evidence")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(journey.REPO / name, tmp_path / name)
    (tmp_path / "runtime").mkdir()
    shutil.copyfile(journey.REPO / "runtime/pyproject.toml", tmp_path / "runtime/pyproject.toml")
    (tmp_path / "primer").symlink_to(journey.REPO / "primer", target_is_directory=True)
    (tmp_path / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (tmp_path / "tests/e2e").mkdir(parents=True)
    for name in ("__init__.py", "test_wiki_w7_t1_lifecycle.py", "test_wiki_w7_t2_lifecycle.py", "test_wiki_w7_t6_lifecycle.py"):
        shutil.copyfile(journey.REPO / "tests/e2e" / name, tmp_path / "tests/e2e" / name)
    (tmp_path / "tests/_support").symlink_to(journey.REPO / "tests/_support", target_is_directory=True)
    trace = valid_evidence()
    for page_name in journey.PAGES:
        page = tmp_path / "docs/wiki" / (page_name + ".md")
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes((journey.REPO / "docs/wiki" / (page_name + ".md")).read_bytes())
        trace["runbook_hashes"][page_name] = hashlib.sha256(page.read_bytes()).hexdigest()
    evidence = tmp_path / "tests/.e2e/w7-t6/trace.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps(trace))
    producer = tmp_path / "tests/.e2e/w7-t2/junit.xml"
    producer.parent.mkdir(parents=True)
    root = ET.Element("testsuites");suite = ET.SubElement(root, "testsuite")
    ET.SubElement(suite, "testcase", classname="tests.e2e.test_wiki_w7_t6_lifecycle", name=journey.CASE)
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


@pytest.mark.asyncio
async def test_actual_filesystem_tools_create_probe_and_delete_response_contract(world, tmp_path):
    from primer.artifact.filesystem import ROOT_MARKER

    _, toolset, _ = world
    entity = {"id": "t6-unit-fs", "provider": "filesystem", "config": {"root": str(tmp_path / "store")}}
    error, created = await _call(toolset, "create_artifact_storage_provider", entity=entity)
    assert not error and created == entity and (tmp_path / "store" / ROOT_MARKER).is_file()
    assert await _call(toolset, "test_artifact_storage_provider", id=entity["id"]) == (False, {"ok": True})
    assert await _call(toolset, "get_artifact_storage_provider", id=entity["id"]) == (False, entity)
    error, deleted = await _call(toolset, "delete_artifact_storage_provider", id=entity["id"])
    assert not error and deleted == {"deleted": True, "id": entity["id"]}
    error, missing = await _call(toolset, "get_artifact_storage_provider", id=entity["id"])
    assert error and missing["type"] == "not-found"
