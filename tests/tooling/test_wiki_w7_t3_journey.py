"""Safe T3 schema/wire/evidence probes; actual user lifecycle remains CI-owned."""

import httpx
import pytest

from tests._support.mock_embeddings import EmbeddingScript, build_app


@pytest.mark.asyncio
async def test_embedding_wire_fixture_preserves_model_batch_order_and_input_dependence():
    script = EmbeddingScript()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(script)), base_url="http://test") as client:
        body = {"model": script.model, "input": ["Cedar instructions", "Quartz instructions", "cedar query"]}
        response = await client.post("/v1/embeddings", json=body)
        assert response.status_code == 200
        data = response.json()
        assert [x["index"] for x in data["data"]] == [0, 1, 2]
        vectors = [x["embedding"] for x in data["data"]]
        assert vectors[0] == vectors[2] != vectors[1]
        assert all(len(x) == 3 for x in vectors)
        assert script.requests == [body]
        response = await client.post("/v1/embeddings", json={"model": script.model, "input": "Cedar query"})
        assert response.json()["data"][0]["embedding"] == vectors[0]


@pytest.mark.parametrize("body", [
    {"model": "wrong", "input": "Cedar"},
    {"model": "w7-embedding", "input": []},
    {"model": "w7-embedding", "input": [1, 2]},
])
def test_embedding_script_does_not_accept_wrong_model_or_non_text_input(body):
    script = EmbeddingScript()
    with pytest.raises(AssertionError):
        script.response(body)
    assert script.requests == []


def stage_items(name):
    from datetime import UTC, datetime
    import json

    from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    values = {
        "reject_enable": {"rejected": True, "tool_name": "enable_collection_search"},
        "peer_enable": {"status": 403, "detail": "owner access required"},
        "reader_import": {"status": 403, "detail": "full access required"},
        "enable": {"search": {"state": "ready", "error": None, "embedder": {"provider_id": "emb", "model": "w7-embedding"}, "vector_store_provider_id": "ssp"}},
        "import": {"created": ["guides", "guides/oncall", "policies", "policies/handbook"], "skipped": [], "overwritten": [], "rejected": []},
        "disable": None,
    }
    value = values[name]
    error = name in {"reject_enable", "peer_enable", "reader_import"}
    output, answer = json.dumps(value), "RESULT:" + json.dumps(value)
    def event(kind, payload):
        return SessionMessageRecord(seq=1, kind=kind, payload=payload, created_at=datetime(2026, 11, 14, tzinfo=UTC)).model_dump(mode="json")
    return [
        event("tool_result", {"call_id": name, "output": output, "error": error}),
        event("assistant_token", {"text": answer}), event("done", {"stop_reason": "stop"}),
        Message(role="assistant", parts=[ToolCallPart(id=name, name="system__call_tool", arguments={
            "toolset_id": "collections_api", "tool_name": journey.SPECS[name][0],
            "arguments": journey.action_arguments("c", "emb", "ssp", "Cedar sample-needle")[name],
        })]).model_dump(mode="json"),
        Message(role="tool", parts=[ToolResultPart(id=name, output=output, error=error)]).model_dump(mode="json"),
        Message(role="assistant", parts=[TextPart(text=answer)]).model_dump(mode="json"),
    ]


@pytest.mark.parametrize("name", ["reject_enable", "peer_enable", "reader_import", "enable", "import", "disable"])
def test_parent_and_actual_tool_results_include_only_the_exact_expected_refusal(name):
    import json
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    records, answer, result = journey.read_stage(stage_items(name), name)
    assert json.loads(answer.removeprefix("RESULT:")) == result
    assert any(r["kind"] == "done" for r in records)
    rule = journey.CollectionActionRule(name, {})
    assert rule.matches({"messages": [{"role": "user", "content": name}]})
    assert rule.emit_tool == "system__call_tool"
    assert rule.matches({"messages": [{"role": "user", "content": name}, {"role": "tool", "tool_call_id": name, "content": json.dumps(result)}]})
    assert rule.emit_tool is None and rule.emit_text == answer


@pytest.mark.parametrize("damage", ["wrong-id", "wrong-status", "missing-error", "extra-error", "root-error", "child-error", "no-parent", "partial-import"])
def test_expected_refusal_does_not_mask_other_tool_or_transcript_failures(damage):
    import json
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    name = "import" if damage == "partial-import" else "peer_enable"
    items = stage_items(name)
    raw = items[4]["parts"][0]
    if damage == "wrong-id":
        raw["id"] = "unrelated"
    elif damage == "wrong-status":
        raw["output"] = json.dumps({"status": 500, "detail": "owner access required"})
    elif damage == "missing-error":
        raw["error"] = False
    elif damage == "extra-error":
        items.append(items[4])
    elif damage in {"root-error", "child-error"}:
        items.append({**items[0], "kind": "error", "payload": {"message": "failed", "delegated": damage == "child-error"}})
    elif damage == "no-parent":
        items = [r for r in items if r.get("kind") != "done"]
    else:
        raw["output"] = json.dumps({"created": ["guides"], "skipped": [], "overwritten": [], "rejected": []})
    with pytest.raises(AssertionError):
        journey.read_stage(items, name)


def valid_evidence():
    import copy
    from primer.bootstrap.seed import default_policy_id
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    identities = {"owner": "o", "peer": "p", "reader": "r"}
    access = {"owners": ["o"], "grants": {"p": "full", "r": "read"}}
    text = "Cedar sample-needle"
    trace = {"case": "test_user_ingests_and_searches_collection_with_real_gates_and_access", "outcome": "passed",
             "source_sha": "source", "runbook_hashes": {p: "hash" for p in journey.PAGES}, "identities": identities,
             "approver": "a", "collection": "c", "text": text, "needle": "sample-needle", "stages": {}, "approvals": [],
             "semantic_backfill": {"status": {"state": "ready", "documents_indexed": 3}, "result": {"mode_used": "semantic", "hits": [{"path": "overview", "excerpt": text}]}},
             "semantic_import": {"reread": {"content": text}, "policy_reread": {"content": "Policy copy\n" + text},
                                 "tree": {"nodes": [{"path": p, "has_children": flag} for p, flag in {
                                     "guides": True, "guides/oncall": False, "policies": True, "policies/handbook": False}.items()]},
                                 "result": {"mode_used": "semantic", "hits": [{"path": "guides/oncall", "excerpt": text}]}},
             "disabled": {"status": {"state": "disabled"}, "literal": {"mode_used": "literal", "hits": [{"path": "guides/oncall"}]}},
             "checks": {k: True for k in ("admin_provider_reads_refused", "unapproved_native_write_has_no_effect", "rejection_and_nonowner_have_no_effect", "collection_deleted", "default_preserved", "private_vector_path")},
             "embedding_requests": [{"model": "w7-embedding", "input": [text, "Cedar on-call escalation"]}]}
    for i, (name, (operation, role, decision)) in enumerate(journey.SPECS.items()):
        items = stage_items(name)
        records, answer, result = journey.read_stage(items, name)
        before = {"search": None, "access": copy.deepcopy(access), "documents": []}
        after = copy.deepcopy(before)
        if name == "enable":
            after["search"] = result["search"]
        if name == "import":
            after["documents"] = [{"path": "guides/oncall"}]
        stage = {"actor": role, "initiator": identities[role], "completed": True, "session_id": "s-" + name,
                 "started": {"initiated_by": {"user_id": identities[role], "role": "user"}}, "records": records,
                 "items": items, "answer": answer, "result": result, "before": before, "after": after}
        trace["stages"][name] = stage
        trace["approvals"].append({"stage": name, "operation": operation, "session_id": stage["session_id"], "decision": decision,
                                   "decision_http": 202, "effect_absent_before": True, "gate_id": "gate-" + str(i), "policy_id": default_policy_id("collections_api", operation),
                                   "pending": {"approval_type": "required", "gate_id": "gate-" + str(i), "policy_id": default_policy_id("collections_api", operation),
                                               "tool_name": operation, "arguments": journey.action_arguments("c", "emb", "ssp", text)[name]}})
    return trace


@pytest.mark.parametrize("damage", ["partial", "wrong-source", "wrong-doc", "same-admin", "missing-stage", "gate-reused", "wrong-gate", "wrong-policy", "wrong-gate-args", "wrong-call-args", "missing-call", "wrong-actor", "early-effect", "wrong-owner", "fallback", "empty-hits", "wrong-body", "wrong-tree", "wrong-backfill-count", "no-embedding", "bad-cleanup"])
def test_required_evidence_rejects_partial_or_false_lifecycle_claims(damage):
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    trace = valid_evidence()
    journey.require_evidence(trace, "source", {p: "hash" for p in journey.PAGES})
    if damage == "partial":trace["outcome"] = "running"
    elif damage == "wrong-source":trace["source_sha"] = "other"
    elif damage == "wrong-doc":trace["runbook_hashes"][journey.PAGES[0]] = "other"
    elif damage == "same-admin":trace["approver"] = "o"
    elif damage == "missing-stage":trace["stages"].pop("disable")
    elif damage == "gate-reused":trace["approvals"][1]["gate_id"] = trace["approvals"][0]["gate_id"]
    elif damage == "wrong-gate":trace["approvals"][0]["pending"]["gate_id"] = "other"
    elif damage == "wrong-policy":
        trace["approvals"][0]["policy_id"] = trace["approvals"][0]["pending"]["policy_id"] = "other"
    elif damage == "wrong-gate-args":trace["approvals"][0]["pending"]["arguments"]["body"]["vector_store_provider_id"] = "other"
    elif damage == "wrong-call-args":trace["stages"]["import"]["items"][3]["parts"][0]["arguments"]["arguments"]["body"]["file"] = "invented"
    elif damage == "missing-call":trace["stages"]["import"]["items"].pop(3)
    elif damage == "wrong-actor":trace["stages"]["peer_enable"]["initiator"] = "o"
    elif damage == "early-effect":trace["stages"]["reject_enable"]["after"]["search"] = {"state": "ready"}
    elif damage == "wrong-owner":trace["stages"]["enable"]["after"]["access"]["owners"] = ["a"]
    elif damage == "fallback":trace["semantic_import"]["result"]["mode_used"] = "fulltext"
    elif damage == "empty-hits":trace["semantic_import"]["result"]["hits"] = []
    elif damage == "wrong-body":trace["semantic_import"]["reread"]["content"] = "invented"
    elif damage == "wrong-tree":trace["semantic_import"]["tree"]["nodes"][0]["has_children"] = False
    elif damage == "wrong-backfill-count":trace["semantic_backfill"]["status"]["documents_indexed"] = 0
    elif damage == "no-embedding":trace["embedding_requests"] = []
    else:trace["checks"]["default_preserved"] = False
    with pytest.raises(AssertionError):
        journey.require_evidence(trace, "source", {p: "hash" for p in journey.PAGES})


def test_import_bytes_have_fixed_metadata_for_exact_gate_and_transcript_binding():
    import base64
    import io
    import zipfile
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    encoded = journey.zip_body("Cedar actual content")
    assert encoded == journey.zip_body("Cedar actual content")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(encoded, validate=True))) as archive:
        assert archive.namelist() == ["Guides/Oncall.md", "Policies/Handbook.txt"]
        assert archive.getinfo("Guides/Oncall.md").date_time == (2026, 1, 1, 0, 0, 0)
        assert archive.read("Guides/Oncall.md") == b"Cedar actual content"
        assert archive.read("Policies/Handbook.txt") == b"Policy copy\nCedar actual content"


def workflow_steps():
    import yaml
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    return yaml.safe_load((journey.REPO / ".github/workflows/e2e.yml").read_text())["jobs"]["e2e"]["steps"]


def test_actual_ci_wiring_retains_one_pytest_run_and_requires_named_live_evidence():
    steps = workflow_steps()
    producer = next(s for s in steps if "uv run pytest tests/e2e" in s.get("run", ""))
    assert producer["run"].count("--junitxml=") == 1
    assert "--junitxml=tests/.e2e/w7-t2/junit.xml" in producer["run"]
    copier = next(s for s in steps if s.get("name") == "Copy live JUnit for W7 collection evidence")
    gate = next(s for s in steps if s.get("name") == "Require executed W7 collection ingestion and search evidence")
    upload = next(s for s in steps if s.get("name") == "Upload W7 collection ingestion and search transcript")
    assert steps.index(producer) < steps.index(copier) < steps.index(gate) < steps.index(upload)
    assert copier["if"] == upload["if"] == "always()"
    assert "if" not in gate and "continue-on-error" not in gate
    assert "uv run --no-sync python -" in gate["run"]
    assert upload["with"]["path"] == "tests/.e2e/w7-t3/"
    assert "w7-t3-collection-search" in upload["with"]["name"]


def test_actual_gate_and_junit_copy_execute_in_a_clean_shell_and_reject_false_acceptance(tmp_path):
    import copy
    import hashlib
    import json
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    from xml.etree import ElementTree as ET
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey

    steps = workflow_steps()
    command = next(s["run"] for s in steps if s.get("name") == "Require executed W7 collection ingestion and search evidence")
    copier = next(s["run"] for s in steps if s.get("name") == "Copy live JUnit for W7 collection evidence")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(journey.REPO / name, tmp_path / name)
    (tmp_path / "runtime").mkdir()
    shutil.copyfile(journey.REPO / "runtime/pyproject.toml", tmp_path / "runtime/pyproject.toml")
    (tmp_path / "primer").symlink_to(journey.REPO / "primer", target_is_directory=True)
    (tmp_path / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (tmp_path / "tests/e2e").mkdir(parents=True)
    for name in ("tests/e2e/__init__.py", *("tests/e2e/test_wiki_w7_" + tier + "_lifecycle.py" for tier in ("t1", "t2", "t3"))):
        shutil.copyfile(journey.REPO / name, tmp_path / name)
    (tmp_path / "tests/_support").symlink_to(journey.REPO / "tests/_support", target_is_directory=True)
    trace = valid_evidence()
    for page in journey.PAGES:
        target = tmp_path / "docs/wiki" / (page + ".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((journey.REPO / "docs/wiki" / (page + ".md")).read_bytes())
        trace["runbook_hashes"][page] = hashlib.sha256(target.read_bytes()).hexdigest()
    evidence = tmp_path / "tests/.e2e/w7-t3/trace.json"
    evidence.parent.mkdir(parents=True)
    producer = tmp_path / "tests/.e2e/w7-t2/junit.xml"
    producer.parent.mkdir(parents=True)
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    ET.SubElement(suite, "testcase", classname="tests.e2e.test_wiki_w7_t3_lifecycle", name=trace["case"])
    good_xml = ET.tostring(root, encoding="unicode")
    producer.write_text(good_xml)
    bare = tmp_path / "clean-python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True, timeout=30)
    uv = shutil.which("uv")
    assert uv
    env = {
        "PATH": os.pathsep.join((str(bare / "bin"), str(Path(uv).parent), "/usr/bin", "/bin")),
        "HOME": os.environ["HOME"], "GITHUB_SHA": "source", "PYTHONNOUSERSITE": "1",
        "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": str(tmp_path / "cache"),
    }
    assert "UV_PROJECT_ENVIRONMENT" not in env and "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env
    def shell(script, environment=env):
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                              cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=45)
    assert shell(copier).returncode == 0
    junit = evidence.with_name("junit.xml")
    assert junit.read_bytes() == producer.read_bytes()
    producer.unlink()
    assert shell(copier).returncode != 0 and not junit.exists()  # a stale prior report cannot survive a missing producer
    producer.write_text(good_xml)
    assert shell(copier).returncode == 0
    evidence.write_text(json.dumps(trace))
    bare_result = shell(command.replace("uv run --no-sync python -", "python3 -", 1))
    assert bare_result.returncode != 0 and "ModuleNotFoundError" in bare_result.stderr
    assert shell(command).returncode == 0
    evidence.unlink()
    assert shell(command).returncode != 0
    evidence.write_text(json.dumps(trace))
    assert shell(command, {**env, "GITHUB_SHA": "wrong-source"}).returncode != 0
    trace["outcome"] = "running"
    evidence.write_text(json.dumps(trace))
    assert shell(command).returncode != 0
    trace["outcome"] = "passed"
    evidence.write_text(json.dumps(trace))
    for damage in ("missing-case", "duplicate", "wrong-class", "wrong-name", "failure", "error", "skipped", "malformed", "missing-report"):
        damaged = ET.fromstring(good_xml)
        case, suite = damaged.find(".//testcase"), damaged.find("testsuite")
        if damage == "missing-case":suite.remove(case)
        elif damage == "duplicate":suite.append(copy.deepcopy(case))
        elif damage == "wrong-class":case.set("classname", "other")
        elif damage == "wrong-name":case.set("name", "other")
        elif damage in {"failure", "error", "skipped"}:ET.SubElement(case, damage)
        junit.write_text("<broken" if damage == "malformed" else ET.tostring(damaged, encoding="unicode"))
        if damage == "missing-report":junit.unlink()
        result = shell(command)
        assert result.returncode != 0, damage
    junit.write_text(good_xml)
    assert shell(command).returncode == 0


def test_private_wiki_loader_and_child_process_config_preserve_isolation(tmp_path, monkeypatch):
    import json
    from tests.e2e import test_wiki_w7_t3_lifecycle as journey
    from primer.knowledge.wiki.pages import load_source_pages

    seen = {}
    def popen(args, **kwargs):
        seen.update(args=args, **kwargs)
        return object()
    monkeypatch.setenv("PRIMER_DB_POSTGRES_HOST", "do-not-inherit")
    monkeypatch.setenv("PRIMER_WIKI_DIR", "/do-not-inherit")
    monkeypatch.setattr(journey.subprocess, "Popen", popen)
    journey.start_server(tmp_path, 51123, object())
    config = json.loads((tmp_path / "config.json").read_text())
    assert config["db"] == {"provider": "sqlite", "config": {"path": str(tmp_path / "data.sqlite")}}
    assert config["auto_bootstrap"] is False and config["auth"]["enabled"] is True
    assert {k: v for k, v in seen["env"].items() if k.startswith("PRIMER_")} == {
        "PRIMER_WIKI_DIR": str(tmp_path / "wiki"),
    }
    assert set(load_source_pages(tmp_path / "wiki")) == {"index", "runbooks", *journey.PAGES}
    assert seen["args"][-1] == str(tmp_path / "config.json")
