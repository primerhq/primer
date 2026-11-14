"""The narrowed unit sweep is written out in five places; they must agree.

CI, the release workflow, the Makefile, AGENTS.md and CONTRIBUTING.md each
spell the same ``pytest tests/ ... --ignore=...`` command. Drift between the
copies is how 692 adapter tests ended up excluded from CI on the stale
premise that they "need a real LLM" while every doc and workflow repeated
it. The exclusion list is compared across all five, and ``tests/llm`` is
pinned as included. ``tests/docs`` is pinned as excluded because it runs in a
dedicated step; CI combines the unit shards' raw coverage with docs coverage.
The local Make coverage recipe still uses seed/append in one environment.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_SWEEP_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "Makefile",
    "AGENTS.md",
    "docs/dev/CONTRIBUTING.md",
)


def _ignored(rel: str) -> frozenset[str]:
    return frozenset(
        re.findall(r"--ignore=(\S+?)(?=[\s\\]|$)", (ROOT / rel).read_text())
    )


def test_all_five_copies_exclude_the_same_directories():
    lists = {rel: _ignored(rel) for rel in _SWEEP_FILES}
    assert all(lists.values()), f"a sweep copy lost its --ignore list: {lists}"
    reference = lists[_SWEEP_FILES[0]]
    drifted = {rel: sorted(v ^ reference) for rel, v in lists.items() if v != reference}
    assert not drifted, (
        f"sweep exclusion lists disagree with {_SWEEP_FILES[0]} "
        f"(symmetric difference shown): {drifted}"
    )


def test_tests_llm_is_part_of_the_sweep():
    for rel in _SWEEP_FILES:
        assert "tests/llm" not in _ignored(rel), (
            f"{rel} excludes tests/llm again; it is adapter unit tests, not "
            "a real-LLM suite, and must run in CI"
        )


def test_docs_are_not_executed_again_by_the_sweep():
    for rel in _SWEEP_FILES:
        assert "tests/docs" in _ignored(rel), (
            f"{rel} puts tests/docs back in the narrowed sweep; the dedicated "
            "docs step already runs it"
        )


def test_ci_combines_docs_and_unit_coverage():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = ci["jobs"]
    docs_run = next(step["run"] for step in jobs["checks"]["steps"] if step.get("name") == "Docs hygiene")
    unit_run = next(
        step["run"] for step in jobs["unit"]["steps"]
        if step.get("name") == "Unit sweep (with coverage)"
    )

    assert "tests/docs/" in docs_run
    assert "--cov=primer" in docs_run
    assert "--cov-report=" in docs_run
    assert "--cov-fail-under=0" in docs_run
    assert "--ignore=tests/docs" in unit_run
    assert "--cov=primer" in unit_run and "--cov-fail-under=0" in unit_run
    assert "--ci-shard=${{ matrix.shard }}" in unit_run
    assert jobs["test"]["if"] == "always()"
    assert jobs["test"]["needs"] == ["checks", "unit"]
    assert any("scripts.tests.ci_coverage" in step.get("run", "") for step in jobs["test"]["steps"])


def test_make_coverage_runs_docs_once_and_combines_coverage():
    makefile = (ROOT / "Makefile").read_text()
    cov_recipe = makefile.split("\ncov:", 1)[1].split("\n\ndocs-hygiene:", 1)[0]
    assert "pytest tests/docs/" in cov_recipe
    assert "--cov=primer" in cov_recipe
    assert "--cov-append" in cov_recipe
