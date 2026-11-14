"""Safe schema/model/evidence probes; the real worker journey runs only in CI."""

import copy
from datetime import UTC, datetime
import json
from types import SimpleNamespace

import jsonschema
import pytest

from primer.api.config import AppConfig
from primer.api.routers.tool_approval import _build_pending_response
from primer.graph._node_refs import _materialise_begin_output
from primer.graph.template import render_input_template
from primer.model.chat import Message, TextPart
from primer.model.graph import Graph, GraphContext, NodeOutput
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import YieldToWorker, new_gate_id
from tests.docs._wiki import tool_entries
from tests.e2e import test_wiki_w7_t2_lifecycle as journey
from tests.toolset.test_system import _ctx, pr, sp, system_toolset  # noqa: F401


@pytest.mark.parametrize("operation", journey.OPERATIONS)
def test_actual_sampled_arguments_validate_against_the_current_catalogue(operation):
    scoped = ("workspace_ext" if operation == "invoke_graph" else "system") + "__" + operation
    schema = {e.scoped_id: e.tool.args_schema for e in tool_entries()}[scoped]
    args = journey.action_arguments("sample-agent", "sample-graph", "sample-profile", "café")[operation]
    jsonschema.validate(args, schema)


def test_action_answer_requires_its_own_actual_success_result_and_does_not_reissue_the_call():
    rule = journey.ActionRule("create_agent", "system__create_agent", {"entity": {"id": "new-agent"}})
    request = {"messages": [{"role": "user", "content": "create_agent"}]}
    assert rule.matches(request) and rule.emit_tool == "system__call_tool"
    assert rule.emit_args["tool_name"] == "create_agent" and rule.emit_tool_call_id == "create_agent"
    for value in ({"id": "one"}, {"id": "different", "description": "café"}):
        resumed = {"messages": [*request["messages"], {"role": "tool", "tool_call_id": "create_agent", "content": json.dumps(value)}]}
        assert rule.matches(resumed) and rule.emit_tool is None
        assert json.loads(rule.emit_text.removeprefix("RESULT:")) == value
    assert not rule.matches({"messages": [{"role": "user", "content": "update_graph"}]})


@pytest.mark.parametrize("value", [{"error": "child failed"}, {"type": "forbidden", "message": "no access"}, [], "invented"])
def test_failed_tool_result_cannot_become_an_acceptance_answer(value):
    rule = journey.ActionRule("invoke_graph", "workspace_ext__invoke_graph", {})
    with pytest.raises(AssertionError):
        rule.matches({"messages": [{"role": "user", "content": "invoke_graph"}, {"role": "tool", "tool_call_id": "invoke_graph", "content": json.dumps(value)}]})


def test_subject_answer_is_derived_from_actual_runtime_input_not_a_fixed_pong():
    rule = journey.PromptAnswerRule()
    for value in ("first café", "different input"):
        assert rule.matches({"messages": [{"role": "user", "content": value}]})
        assert rule.emit_text == "AGENT-ANSWER:" + value
    with pytest.raises(AssertionError):
        rule.matches({"messages": []})


def test_pending_card_check_refuses_a_different_operation_or_occurrence():
    args = {"entity": {"id": "sample-agent"}}
    metadata = {"original_call": {"id": "one-call", "name": "create_agent", "arguments": args}, "policy_id": "expected-policy", "approval_type": "required", "gate_id": new_gate_id()}
    pending = _build_pending_response({}, SimpleNamespace(parked_at=datetime.now(UTC)), {"resume_metadata": metadata}).model_dump(mode="json")
    assert pending["toolset_id"] is None
    journey.check_pending(pending, "create_agent", args, "expected-policy")
    for key, value in (("tool_name", "update_agent"), ("policy_id", "other-policy"), ("approval_type", "policy"), ("gate_id", None), ("arguments", {})):
        with pytest.raises(AssertionError):
            journey.check_pending({**pending, key: value}, "create_agent", args, "expected-policy")


@pytest.mark.asyncio
async def test_actual_call_tool_gate_projects_the_exact_pending_card_checked_by_the_journey(system_toolset, sp):
    await sp.get_storage(ToolApprovalPolicy).create(ToolApprovalPolicy(
        id="t2-create-agent", toolset_id="system", tool_name="create_agent", approval=RequiredApprovalConfig(),
    ))
    args = journey.action_arguments("a", "g", "p", "café")["create_agent"]
    with pytest.raises(YieldToWorker) as raised:
        await system_toolset.call(tool_name="call_tool", arguments={"toolset_id": "system", "tool_name": "create_agent", "arguments": args}, ctx=_ctx())
    metadata = raised.value.yielded.resume_metadata
    assert metadata["via_call_tool"] == {"toolset_id": "system", "tool_name": "create_agent"}
    pending = _build_pending_response({}, SimpleNamespace(parked_at=datetime.now(UTC)), {"resume_metadata": metadata}).model_dump(mode="json")
    journey.check_pending(pending, "create_agent", args, "t2-create-agent")


@pytest.mark.parametrize("mode", ["binding", "nested"])
def test_real_begin_materializer_and_template_accept_both_invocation_input_modes(mode):
    graph = Graph.model_validate(journey.action_arguments("a", "g", "p", "café")["create_graph"]["entity"])
    graph.assert_runnable()
    messages = [Message(role="user", parts=[TextPart(text="café")])]
    begin = _materialise_begin_output("café" if mode == "binding" else messages, messages)
    context = GraphContext(initial_input="café" if mode == "binding" else messages, iteration=1, nodes={"begin": begin})
    assert render_input_template(graph.nodes[1].input_template, context=context) == "café"
    context.nodes["answer"] = NodeOutput(text="AGENT-ANSWER:café", iteration=1)
    assert render_input_template(graph.nodes[2].output_template, context=context) == "GRAPH-ANSWER:AGENT-ANSWER:café"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["binding", "nested"])
async def test_sampled_graph_really_executes_and_persists_its_end_in_both_modes(tmp_path, mode):
    from primer.graph.base import _GraphEndOutputEvent
    from primer.graph.invoke_graph import GraphInvocationServices, run_invoke_graph
    from primer.graph.workspace_executor import WorkspaceGraphExecutor
    from primer.model.agent import Agent
    from primer.model.chat import Done, TextDelta
    from tests.graph.test_workspace_executor import _FakeLLM, _make_state_repo, _model

    actions = journey.action_arguments("a", "g", "p", "café")
    graph = Graph.model_validate(actions["create_graph"]["entity"])
    agent = Agent.model_validate(actions["create_agent"]["entity"])
    repo = await _make_state_repo(tmp_path)
    llm = _FakeLLM(scripts=[[TextDelta(index=0, text="AGENT-ANSWER:café"), Done(stop_reason="stop", raw_reason="stop")]])

    async def resolve_agent(_):
        return agent

    async def resolve_llm(_):
        return llm, _model()

    async def resolve_graph(_):
        return graph

    executors = []

    async def build_child_executor(*, graph, gsid):
        executor = WorkspaceGraphExecutor(
            graph=graph, agent_resolver=resolve_agent, llm_resolver=resolve_llm,
            state_repo=repo, graph_session_id=gsid, graph_input="café" if mode == "binding" else None,
        )
        executors.append(executor)
        return executor

    if mode == "binding":
        executor = await build_child_executor(graph=graph, gsid="binding")
        events = [event async for event in executor.invoke([])]
        outputs = [event.text for event in events if isinstance(event, _GraphEndOutputEvent)]
        assert outputs == ["GRAPH-ANSWER:AGENT-ANSWER:café"]
    else:
        services = GraphInvocationServices(resolve_graph=resolve_graph, build_child_executor=build_child_executor,
                                           session_id="s", workspace_id="ws-test", graph_session_id="nested")
        assert await run_invoke_graph(graph_id="g", graph_input="café", services=services, tool_call_id="one") == "GRAPH-ANSWER:AGENT-ANSWER:café"
    state = await executors[0].load_state()
    assert state["status"] == "ended" and state["ended_reason"] == "completed"
    assert llm.calls[0]["messages"][-1].parts[0].text == "café"


def valid_evidence():
    trace = {"case": "test_user_authors_and_runs_agent_and_graph_with_real_approvals", "outcome": "passed", "source_sha": "source", "runbook_hashes": {p: "hash" for p in journey.PAGES},
             "role": "user", "user_id": "person", "approver": {"role": "admin", "user_id": "approver"}, "input": "café", "approvals": [], "stages": {},
             "graph_session": {"completed": True, "end_output": "GRAPH-ANSWER:AGENT-ANSWER:café"}, "deleted_agent": True, "deleted_graph": True, "default_preserved": True}
    for name in journey.OPERATIONS:
        result = {"output": ("GRAPH-ANSWER:" if name == "invoke_graph" else "") + "AGENT-ANSWER:café"}
        trace["stages"][name] = {"session_id": "s-" + name, "initiator": "person", "completed": True, "result": result, "answer": "RESULT:" + json.dumps(result)}
    trace["approvals"] = [{"operation": name, "session_id": "s-" + name, "policy_id": "policy-" + name, "type": "required", "gate_id": new_gate_id(), "effect_absent_before_approval": True, "decision_http": 202} for name in journey.GATED]
    return trace


def write_junit(evidence):
    from xml.etree import ElementTree as ET

    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    ET.SubElement(suite, "testcase", classname=journey.__name__, name=valid_evidence()["case"])
    target = evidence.with_name("junit.xml")
    ET.ElementTree(root).write(target, encoding="unicode")
    return target


def test_ci_evidence_accepts_only_a_complete_source_bound_control():
    trace = valid_evidence()
    journey.require_evidence(trace, "source", trace["runbook_hashes"])


def test_exact_workflow_step_executes_the_guard_and_rejects_missing_or_partial_evidence(tmp_path, monkeypatch):
    import hashlib
    from xml.etree import ElementTree as ET
    import yaml

    workflow = yaml.safe_load((journey.REPO / ".github/workflows/e2e.yml").read_text())
    steps = workflow["jobs"]["e2e"]["steps"]
    step = next(s for s in steps if s.get("name") == "Require executed W7 agent and graph evidence")
    assert "if" not in step and "continue-on-error" not in step
    script = step["run"].split("\n", 1)[1].rsplit("\nPY", 1)[0]
    trace = valid_evidence()
    trace["runbook_hashes"] = {}
    for path in journey.PAGES:
        page = tmp_path / "docs/wiki" / (path + ".md")
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes((journey.REPO / "docs/wiki" / (path + ".md")).read_bytes())
        trace["runbook_hashes"][path] = hashlib.sha256(page.read_bytes()).hexdigest()
    evidence = tmp_path / "tests/.e2e/w7-t2/trace.json"
    evidence.parent.mkdir(parents=True)
    junit = write_junit(evidence)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "source")
    with pytest.raises(FileNotFoundError):
        exec(compile(script, "<actual-ci-evidence-step>", "exec"), {})
    evidence.write_text(json.dumps(trace))
    exec(compile(script, "<actual-ci-evidence-step>", "exec"), {})
    trace["outcome"] = "running"
    evidence.write_text(json.dumps(trace))
    with pytest.raises(AssertionError):
        exec(compile(script, "<actual-ci-evidence-step>", "exec"), {})
    trace["outcome"] = "passed"
    evidence.write_text(json.dumps(trace))
    passed = junit.read_text()
    for damage in ("missing-case", "duplicate", "failure", "error", "skipped"):
        root = ET.fromstring(passed)
        case = root.find(".//testcase")
        suite = root.find("testsuite")
        if damage == "missing-case":
            suite.remove(case)
        elif damage == "duplicate":
            suite.append(copy.deepcopy(case))
        else:
            ET.SubElement(case, damage)
        junit.write_text(ET.tostring(root, encoding="unicode"))
        with pytest.raises(AssertionError):
            exec(compile(script, "<actual-ci-evidence-step>", "exec"), {})
    junit.write_text("<broken")
    with pytest.raises(ET.ParseError):
        exec(compile(script, "<actual-ci-evidence-step>", "exec"), {})
    junit.unlink()
    with pytest.raises(FileNotFoundError):
        exec(compile(script, "<actual-ci-evidence-step>", "exec"), {})
    producer = next(s for s in steps if "uv run pytest tests/e2e" in s.get("run", ""))
    assert "--junitxml=tests/.e2e/w7-t2/junit.xml" in producer["run"]
    upload = next(s for s in steps if s.get("name") == "Upload W7 agent and graph transcript")
    assert upload["if"] == "always()" and upload["with"]["path"] == "tests/.e2e/w7-t2/"


def test_actual_workflow_launcher_uses_project_dependencies_in_a_clean_shell(tmp_path):
    import hashlib
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    import yaml

    workflow = yaml.safe_load((journey.REPO / ".github/workflows/e2e.yml").read_text())
    command = next(s["run"] for s in workflow["jobs"]["e2e"]["steps"] if s.get("name") == "Require executed W7 agent and graph evidence")
    # A scratch project keeps synthetic controls away from real CI evidence.
    # There is no activated project venv or inherited Python import path.
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(journey.REPO / name, tmp_path / name)
    (tmp_path / "runtime").mkdir()
    shutil.copyfile(journey.REPO / "runtime/pyproject.toml", tmp_path / "runtime/pyproject.toml")
    (tmp_path / "primer").symlink_to(journey.REPO / "primer", target_is_directory=True)
    (tmp_path / "tests/e2e").mkdir(parents=True)
    for name in ("tests/__init__.py", "tests/e2e/__init__.py"):
        source = journey.REPO / name
        if source.exists():
            shutil.copyfile(source, tmp_path / name)
    (tmp_path / "tests/_support").symlink_to(journey.REPO / "tests/_support", target_is_directory=True)
    for name in ("test_wiki_w7_t1_lifecycle.py", "test_wiki_w7_t2_lifecycle.py"):
        (tmp_path / "tests/e2e" / name).symlink_to(journey.REPO / "tests/e2e" / name)
    trace = valid_evidence()
    trace["runbook_hashes"] = {}
    for page in journey.PAGES:
        target = tmp_path / "docs/wiki" / (page + ".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((journey.REPO / "docs/wiki" / (page + ".md")).read_bytes())
        trace["runbook_hashes"][page] = hashlib.sha256(target.read_bytes()).hexdigest()
    evidence = tmp_path / "tests/.e2e/w7-t2/trace.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps(trace))
    junit = write_junit(evidence)
    bare = tmp_path / "clean-python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True, timeout=30)
    uv = shutil.which("uv")
    assert uv, "the project test environment requires uv"
    env = {
        "PATH": os.pathsep.join((str(bare / "bin"), str(Path(uv).parent), "/usr/bin", "/bin")),
        "HOME": os.environ["HOME"], "GITHUB_SHA": "source", "PYTHONNOUSERSITE": "1",
        "UV_PROJECT_ENVIRONMENT": sys.prefix, "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
    }

    def shell(script, environment=env):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=45,
        )

    old = shell(command.replace("uv run --no-sync python -", "python3 -", 1))
    assert old.returncode != 0 and "ModuleNotFoundError: No module named 'httpx'" in old.stderr
    valid = shell(command)
    assert valid.returncode == 0, valid.stderr
    evidence.unlink()
    missing = shell(command)
    assert missing.returncode != 0 and "FileNotFoundError" in missing.stderr
    evidence.write_text(json.dumps(trace))
    wrong_source = shell(command, {**env, "GITHUB_SHA": "other-source"})
    assert wrong_source.returncode != 0 and "AssertionError" in wrong_source.stderr
    trace["outcome"] = "running"
    evidence.write_text(json.dumps(trace))
    partial = shell(command)
    assert partial.returncode != 0 and "AssertionError" in partial.stderr
    trace["outcome"] = "passed"
    evidence.write_text(json.dumps(trace))
    junit.unlink()
    missing_junit = shell(command)
    assert missing_junit.returncode != 0 and "FileNotFoundError" in missing_junit.stderr
    from xml.etree import ElementTree as ET

    write_junit(evidence)
    root = ET.parse(junit)
    ET.SubElement(root.find(".//testcase"), "skipped")
    root.write(junit, encoding="unicode")
    skipped = shell(command)
    assert skipped.returncode != 0 and "AssertionError" in skipped.stderr


@pytest.mark.parametrize("damage", ["missing", "precondition", "source", "hash", "role", "approver", "operation", "approval", "premature_effect", "wrong_session", "incomplete", "invented_answer", "graph", "delete", "default"])
def test_ci_gate_fails_closed_on_collection_precondition_and_partial_execution(damage):
    trace = copy.deepcopy(valid_evidence())
    hashes = trace["runbook_hashes"].copy()
    if damage == "missing":
        trace = {}
    elif damage == "precondition":
        trace["outcome"] = "precondition_failed"
    elif damage == "source":
        trace["source_sha"] = "other"
    elif damage == "hash":
        trace["runbook_hashes"] = {}
    elif damage == "role":
        trace["role"] = "admin"
    elif damage == "approver":
        trace["approver"]["user_id"] = "person"
    elif damage == "operation":
        trace["stages"].pop("invoke_graph")
    elif damage == "approval":
        trace["approvals"].pop()
    elif damage == "premature_effect":
        trace["approvals"][0]["effect_absent_before_approval"] = False
    elif damage == "wrong_session":
        trace["approvals"][0]["session_id"] = "other"
    elif damage == "incomplete":
        trace["stages"]["invoke_agent"]["completed"] = False
    elif damage == "invented_answer":
        trace["stages"]["invoke_agent"]["result"] = {"output": "PONG"}
    elif damage == "graph":
        trace["graph_session"]["end_output"] = "PONG"
    elif damage == "delete":
        trace["deleted_graph"] = False
    else:
        trace["default_preserved"] = False
    with pytest.raises((AssertionError, KeyError, ValueError)):
        journey.require_evidence(trace, "source", hashes)


def test_scoped_wiki_and_child_process_are_isolated_without_starting_a_server(tmp_path, monkeypatch):
    from primer.knowledge.wiki.build import build_wiki
    from primer.knowledge.wiki.pages import load_source_pages

    captures = []
    monkeypatch.setenv("PRIMER_DB_URL", "must-not-inherit")
    monkeypatch.setenv("PRIMER_WIKI_DIR", "must-not-inherit")
    monkeypatch.setattr(journey.subprocess, "Popen", lambda *args, **kwargs: captures.append((args, kwargs)))
    journey.start_server(tmp_path, 18701, "fake-log")
    args, kwargs = captures[0]
    assert args[0][-2:] == ["--config", str(tmp_path / "config.json")]
    config = AppConfig.model_validate_json((tmp_path / "config.json").read_text())
    assert config.auth.enabled and not config.auto_bootstrap and config.port == 18701
    assert config.db.config.path == tmp_path / "data.sqlite"
    assert {k: v for k, v in kwargs["env"].items() if k.startswith("PRIMER_")} == {
        "PRIMER_WIKI_DIR": str(tmp_path / "wiki"), "PRIMER_AI_DOCS_DIR": str(tmp_path / "legacy-empty"),
    }
    source = load_source_pages(tmp_path / "wiki")
    assert set(source) == {"index", "runbooks", *journey.PAGES}
    built = build_wiki(tmp_path / "wiki", tool_entries(), legacy_pages={})
    for path in journey.PAGES:
        assert source[path].body in built.pages[path].body
        assert (tmp_path / "wiki" / (path + ".md")).read_bytes() == (journey.REPO / "docs/wiki" / (path + ".md")).read_bytes()
