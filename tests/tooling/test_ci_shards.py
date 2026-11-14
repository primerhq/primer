"""Partition completeness, real pytest/xdist selection, and fail-closed coverage."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import coverage
import pytest
import yaml

from scripts.tests.ci_coverage import combine, main, validate_jobs, verify_inputs
from scripts.tests.ci_shards import WEIGHTS, digest, partition, validate_reports
from scripts.tests.ci_weights import from_junit

ROOT = Path(__file__).resolve().parents[2]
SHA = "reviewed-ci-source"


def reports_for(ids, timings=None, count=2):
    assigned, _ = partition(ids, timings or {}, count)
    return [{
        "shard": i, "shards": count, "source_sha": SHA,
        "weights_sha256": hashlib.sha256(WEIGHTS.read_bytes()).hexdigest(),
        "inventory_count": len(ids), "inventory_sha256": digest(ids),
        "selected": sorted(n for n in ids if assigned[n.split("::", 1)[0]] == i),
        "complete": True, "exitstatus": 0,
    } for i in range(count)]


def validate(reports):
    validate_reports(reports, count=2, source_sha=SHA, weights_sha=hashlib.sha256(WEIGHTS.read_bytes()).hexdigest())


def test_partition_includes_new_modules_new_parameter_ids_and_zero_cost_skips_once():
    ids = ["tests/test_heavy.py::test_x[a]", "tests/test_heavy.py::test_x[new]", "tests/test_new.py::test_a", "tests/test_skip.py::test_a"]
    timings = {"tests/test_heavy.py": {"seconds": 20, "tests": 1}, "tests/test_skip.py": {"seconds": 0, "tests": 1}}
    assigned, loads = partition(ids, timings, 2)
    assert assigned == partition(list(reversed(ids)), timings, 2)[0]
    assert set(assigned) == {n.split("::")[0] for n in ids}
    assert max(loads) == 40  # Growth in a measured module is accounted for.
    validate(reports_for(ids, timings))


def test_longest_first_balances_heavy_api_modules_instead_of_grouping_the_subsystem():
    timings = {f"tests/api/test_{i}.py": {"seconds": cost, "tests": 1} for i, cost in enumerate([100, 90, 20, 10])}
    assigned, loads = partition([p + "::test_a" for p in timings], timings, 2)
    assert assigned["tests/api/test_0.py"] != assigned["tests/api/test_1.py"]
    assert loads == [110, 110]


def test_entire_measured_collection_partitions_completely_with_balanced_cost():
    timings = json.loads(WEIGHTS.read_text())["modules"]
    ids = [f"{path}::item-{i}" for path, row in timings.items() for i in range(row["tests"])]
    reports = reports_for(ids, timings, count=4)
    validate_reports(reports, count=4, source_sha=SHA, weights_sha=hashlib.sha256(WEIGHTS.read_bytes()).hexdigest())
    _, loads = partition(ids, timings, 4)
    assert max(loads) - min(loads) < 1
    assert len({path for path in timings if path.startswith("tests/api/")}) > 100


@pytest.mark.parametrize("corrupt", ["missing", "duplicate", "failed", "status", "incomplete_union", "overlap", "drift", "source", "weights", "split_module"])
def test_report_gate_refuses_partial_stale_failed_or_overlapping_inputs(corrupt):
    ids = ["tests/test_a.py::test_a", "tests/test_a.py::test_b", "tests/test_b.py::test_a", "tests/test_b.py::test_b"]
    reports = reports_for(ids)
    if corrupt == "missing": reports.pop()
    elif corrupt == "duplicate": reports[1] = deepcopy(reports[0])
    elif corrupt == "failed": reports[0]["complete"] = False
    elif corrupt == "status": reports[0]["exitstatus"] = 1
    elif corrupt == "incomplete_union": reports[0]["selected"].pop()
    elif corrupt == "overlap": reports[1]["selected"].append(reports[0]["selected"][0])
    elif corrupt == "drift": reports[0]["inventory_sha256"] = "stale"
    elif corrupt == "source": reports[0]["source_sha"] = "old"
    elif corrupt == "weights": reports[0]["weights_sha256"] = "old"
    elif corrupt == "split_module": reports[1]["selected"].append(reports[0]["selected"].pop())
    with pytest.raises(ValueError): validate(reports)


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", None])
def test_failed_or_skipped_producer_cannot_be_hidden_by_successful_remaining_coverage(state):
    with pytest.raises(ValueError): validate_jobs({"checks": {"result": "success"}, "unit": {"result": state}})
    with pytest.raises(ValueError): validate_jobs({"unit": {"result": "success"}})


def write_coverage(path, source, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = coverage.CoverageData(basename=str(path))
    data.add_lines({str(source): set(lines)})
    data.write()


def test_artifacts_require_docs_and_every_shards_report_and_raw_data(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("x = 1\ny = 2\n")
    reports = reports_for(["tests/test_a.py::test_a", "tests/test_b.py::test_a"])
    docs = tmp_path / "artifacts/ci-coverage-docs"
    write_coverage(docs / "coverage", source, [1])
    for report in reports:
        folder = tmp_path / f"artifacts/ci-coverage-unit-{report['shard']}"
        write_coverage(folder / "coverage", source, [2])
        (folder / "report.json").write_text(json.dumps(report))
    root = docs.parent
    assert len(verify_inputs(root, source_sha=SHA, count=2)) == 3
    raw = root / "ci-coverage-unit-1/coverage"
    raw.unlink()
    with pytest.raises(ValueError, match="missing coverage"): verify_inputs(root, source_sha=SHA, count=2)
    write_coverage(raw, source, [2])
    (root / "ci-coverage-unit-1/report.json").unlink()
    with pytest.raises(FileNotFoundError): verify_inputs(root, source_sha=SHA, count=2)
    (root / "unexpected").mkdir()
    with pytest.raises(ValueError, match="artifact"): verify_inputs(root, source_sha=SHA, count=2)


def test_raw_coverage_union_reaches_gate_and_a_partial_union_still_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "example.py"
    source.write_text("x = 1\ny = 2\n")
    docs, unit = tmp_path / "docs", tmp_path / "unit"
    write_coverage(docs, source, [1])
    write_coverage(unit, source, [2])
    assert combine([docs, unit], output=tmp_path / ".coverage") == 100
    xml = (tmp_path / "coverage.xml").read_text()
    assert 'line-rate="1"' in xml
    with pytest.raises(ValueError, match="below 80"):
        combine([docs], output=tmp_path / ".coverage-partial")


def test_coverage_gate_keeps_pytest_cov_rounding_at_the_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "threshold.py"
    source.write_text("x = 1\n" * 1000)
    raw = tmp_path / "raw"
    write_coverage(raw, source, range(1, 800))
    assert combine([raw], output=tmp_path / ".coverage") == 79.9


def test_aggregate_entrypoint_requires_producers_and_combines_docs_with_all_four_shards(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "example.py"
    source.write_text("x = 1\ny = 2\n")
    root = tmp_path / "artifacts"
    write_coverage(root / "ci-coverage-docs/coverage", source, [1])
    ids = [f"tests/test_{i}.py::test_a" for i in range(4)]
    for report in reports_for(ids, count=4):
        folder = root / f"ci-coverage-unit-{report['shard']}"
        write_coverage(folder / "coverage", source, [2])
        (folder / "report.json").write_text(json.dumps(report))
    needs = {"checks": {"result": "success"}, "unit": {"result": "success"}}
    args = ["ci_coverage", "--artifacts", str(root), "--source-sha", SHA, "--needs"]
    monkeypatch.setattr(sys, "argv", [*args, json.dumps(needs)])
    main()
    assert (tmp_path / "coverage.xml").is_file()
    (tmp_path / "coverage.xml").unlink()
    needs["unit"]["result"] = "failure"
    monkeypatch.setattr(sys, "argv", [*args, json.dumps(needs)])
    with pytest.raises(ValueError, match="must succeed"):
        main()
    assert not (tmp_path / "coverage.xml").exists()


@pytest.mark.parametrize("workers", ["0", "2"])
def test_real_pytest_selection_and_fixture_scope_survive_sharding(tmp_path, workers):
    # No application tests/processes: a tiny independent pytest project.
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    excluded: outside this selection\n")
    (tmp_path / "toy.py").write_text("def value():\n    return 1\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    for suffix in ("a", "b", "new"):
        (tests / f"test_{suffix}.py").write_text(
            "import pytest\nfrom toy import value\n"
            "@pytest.fixture(scope='module')\ndef shared(): return []\n"
            "def test_first(shared): assert value() == 1; shared.append(1)\n"
            "def test_second(shared): assert shared == [1]\n"
            "@pytest.mark.excluded\ndef test_not_selected(): assert False\n"
        )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_CORE_", "COVERAGE_"))}
    env.update(PYTHONPATH=str(ROOT), GITHUB_SHA=SHA, PYTHONDONTWRITEBYTECODE="1")
    reports = []
    for index in range(2):
        report = tmp_path / f"report-{index}.json"
        raw = tmp_path / f"raw-{index}/coverage"
        env["COVERAGE_FILE"] = str(raw)
        done = subprocess.run([
            sys.executable, "-m", "pytest", "tests", "-q", "-n", workers, "--dist=loadscope", "-m", "not excluded",
            "-p", "scripts.tests.ci_shards", f"--ci-shard={index}", "--ci-shards=2", f"--ci-shard-report={report}",
            "--cov=toy", "--cov-report=", "--cov-fail-under=0",
        ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stdout + done.stderr
        reports.append(json.loads(report.read_text()))
        data = coverage.CoverageData(basename=str(raw))
        data.read()
        assert str(tmp_path / "toy.py") in data.measured_files()
    validate(reports)
    assert sum(len(r["selected"]) for r in reports) == 6


@pytest.mark.parametrize("source", ["def test_fails(): assert False\n", "raise RuntimeError('collection failed')\n"])
def test_real_failure_or_collection_error_never_writes_a_success_report(tmp_path, source):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_failure.py").write_text(source)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_CORE_", "COVERAGE_"))}
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    report = tmp_path / "report.json"
    done = subprocess.run([
        sys.executable, "-m", "pytest", "-q", "-n2", "-p", "scripts.tests.ci_shards",
        "--ci-shard=0", "--ci-shards=1", f"--ci-shard-report={report}",
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0, done.stdout + done.stderr
    body = json.loads(report.read_text())
    assert body["complete"] is False and body["exitstatus"] != 0


def test_workflow_keeps_fixtures_selection_timeouts_extras_and_strict_test_gate():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    unit = jobs["unit"]
    assert unit["strategy"] == {"fail-fast": False, "matrix": {"shard": [0, 1, 2, 3]}}
    assert unit["timeout-minutes"] == 90
    for name in ("unit", "checks"):
        sync = next(s["run"] for s in jobs[name]["steps"] if s.get("name") == "Sync")
        assert "uv sync --all-extras --dev --group docs" in sync and "uv pip install --no-deps ./runtime" in sync
        upload = next(s for s in jobs[name]["steps"] if s.get("uses", "").startswith("actions/upload-artifact"))
        assert upload["if"] == "always()" and upload["with"]["if-no-files-found"] == "error"
        assert not upload.get("continue-on-error")
    assert jobs["test"]["if"] == "always()" and jobs["test"]["needs"] == ["checks", "unit"]
    command = next(s for s in jobs["test"]["steps"] if s.get("name") == "Verify complete selection and combined coverage")
    assert "scripts.tests.ci_coverage" in command["run"] and command["env"]["CI_NEEDS"] == "${{ toJSON(needs) }}"
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "-n auto --dist loadscope --timeout=300 --timeout-method=thread" in pyproject


def test_timing_import_preserves_class_modules_and_refuses_failed_baseline(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("")
    xml = tmp_path / "input.xml"
    xml.write_text('<testsuites><testsuite><testcase classname="tests.test_a.TestGroup" name="test_a[x]" time="2.5"/></testsuite></testsuites>')
    baseline = from_junit(xml, tmp_path, "source-run")
    assert baseline["modules"] == {"tests/test_a.py": {"seconds": 2.5, "tests": 1}}
    xml.write_text(xml.read_text().replace("/>", "><failure/></testcase>", 1))
    with pytest.raises(ValueError): from_junit(xml, tmp_path, "source-run")
