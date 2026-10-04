"""The narrowed unit sweep is written out in five places; they must agree.

CI, the release workflow, the Makefile, AGENTS.md and CONTRIBUTING.md each
spell the same ``pytest tests/ ... --ignore=...`` command. Drift between the
copies is how 692 adapter tests ended up excluded from CI on the stale
premise that they "need a real LLM" while every doc and workflow repeated
it. The exclusion list is compared across all five, and ``tests/llm`` is
pinned as included: it is adapter unit tests with every request intercepted
in-process (tests/llm/conftest.py fails any test that reaches the network).
"""

from __future__ import annotations

import re
from pathlib import Path

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
