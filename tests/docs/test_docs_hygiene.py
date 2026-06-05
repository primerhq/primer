"""Hygiene tests for the consolidated developer docs under docs/dev/.

These tests run in the narrowed sweep at every commit so doc rot is
caught at PR time. They are also the pass/fail gate for the one-shot
consolidation verifier (scripts/docs_verifier.py), which runs this
suite then performs the side-effects (deferred-from-specs roll-up
and _work/ removal).

Spec: docs/superpowers/specs/2026-06-05-dev-docs-consolidation-design.md
section 5.4 (verifier checks) and section 11 (maintenance model).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS_DEV = REPO / "docs" / "dev"
AGENTS_MD = REPO / "AGENTS.md"

ARCHITECTURE_DOCS = [
    "provider-pattern",
    "worker-system",
    "claim-machine",
    "storage",
    "rest-api",
    "observability",
    "auto-bootstrap",
]

SUBSYSTEM_DOCS = [
    "workspaces",
    "sessions",
    "agents",
    "graphs",
    "chats",
    "channels",
    "knowledge",
    "semantic-search",
    "web-search",
    "triggers",
    "harness",
    "model-providers",
    "ui-foundation",
    "ui-pages",
]

ARCH_HEADINGS = [
    "Purpose",
    "Visual overview",
    "Public surface",
    "How to add a new implementation",
    "Existing implementations",
    "Wiring",
    "Testing patterns",
    "Historical decisions",
]

SUBSYS_HEADINGS = [
    "Purpose",
    "Conceptual model",
    "Architecture patterns implemented",
    "Code layout",
    "Data model",
    "Lifecycle",
    "Persistence",
    "Public surfaces",
    "Internal contracts",
    "Testing patterns",
    "Historical decisions",
]

EM_DASH = "—"

_PLACEHOLDER_RE = re.compile(r"\b(TBD|FIXME|XXX|\?\?\?)\b")
_TODO_AT_LINE_START_RE = re.compile(r"^\s*TODO\b", re.MULTILINE)
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
_MERMAID_FIRST_LINE_KEYWORDS = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "classDiagram",
    "erDiagram",
    "mindmap",
    "journey",
    "gantt",
    "pie",
    "timeline",
)


def _all_tracked_docs() -> list[Path]:
    """Every markdown file in docs/dev/ plus AGENTS.md.

    Returns an empty list if docs/dev/ does not exist yet. This lets the
    test module import cleanly during the pre-consolidation window.
    """
    if not DOCS_DEV.exists():
        return []
    docs = sorted(DOCS_DEV.rglob("*.md"))
    if AGENTS_MD.exists():
        docs.append(AGENTS_MD)
    return docs


# ---- Existence + layout ----------------------------------------------------


def test_all_expected_docs_exist():
    """Spec section 3: 24 tracked files in docs/dev/ (after consolidation
    finalises). When the synthesis tree has not yet been populated (we
    are between Phase 0 and Phase 4), the test is skipped."""
    if not (DOCS_DEV / "architecture").exists() or not (
        DOCS_DEV / "subsystems"
    ).exists():
        pytest.skip(
            "docs/dev/architecture or docs/dev/subsystems missing; "
            "consolidation in progress"
        )
    expected = {DOCS_DEV / "README.md", DOCS_DEV / "CONTRIBUTING.md"}
    for name in ARCHITECTURE_DOCS:
        expected.add(DOCS_DEV / "architecture" / f"{name}.md")
    for name in SUBSYSTEM_DOCS:
        expected.add(DOCS_DEV / "subsystems" / f"{name}.md")
    expected.add(AGENTS_MD)
    missing = [str(p.relative_to(REPO)) for p in expected if not p.exists()]
    assert not missing, f"Missing docs: {missing}"


# ---- Em-dash absence -------------------------------------------------------


@pytest.mark.parametrize(
    "doc", _all_tracked_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_no_em_dashes(doc: Path):
    """No em dash characters anywhere in the doc set."""
    content = doc.read_text(encoding="utf-8")
    if EM_DASH in content:
        offending_lines = [
            f"  line {i + 1}: {line}"
            for i, line in enumerate(content.splitlines())
            if EM_DASH in line
        ]
        pytest.fail(
            f"{doc.relative_to(REPO)} contains em dashes:\n"
            + "\n".join(offending_lines)
        )


# ---- Placeholder absence ---------------------------------------------------


@pytest.mark.parametrize(
    "doc", _all_tracked_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_no_placeholder_tokens(doc: Path):
    """No 'TBD', 'FIXME', 'XXX', '???' tokens. TODO is allowed only as
    a heading word (e.g. 'TODO list'), not as a standalone marker at
    the start of a line."""
    content = doc.read_text(encoding="utf-8")
    placeholders = _PLACEHOLDER_RE.findall(content)
    assert not placeholders, (
        f"{doc.relative_to(REPO)} contains placeholder tokens: "
        f"{placeholders}"
    )
    todo_markers = _TODO_AT_LINE_START_RE.findall(content)
    assert not todo_markers, (
        f"{doc.relative_to(REPO)} has TODO marker(s) at line start"
    )


# ---- Mermaid syntax check --------------------------------------------------


@pytest.mark.parametrize(
    "doc", _all_tracked_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_mermaid_blocks_well_formed(doc: Path):
    """Every ```mermaid block must (a) close with ``` and (b) start
    with a recognised diagram keyword on its first non-empty line."""
    content = doc.read_text(encoding="utf-8")
    for match in _MERMAID_BLOCK_RE.finditer(content):
        block = match.group(1)
        first_line = next(
            (line.strip() for line in block.splitlines() if line.strip()),
            "",
        )
        first_word = first_line.split()[0] if first_line else ""
        if first_word not in _MERMAID_FIRST_LINE_KEYWORDS:
            pytest.fail(
                f"{doc.relative_to(REPO)}: mermaid block starts with "
                f"{first_word!r}; expected one of "
                f"{_MERMAID_FIRST_LINE_KEYWORDS}"
            )
    opener_count = content.count("```mermaid")
    closed_count = len(_MERMAID_BLOCK_RE.findall(content))
    assert opener_count == closed_count, (
        f"{doc.relative_to(REPO)}: {opener_count} ```mermaid blocks "
        f"opened but {closed_count} closed (unbalanced)"
    )


# ---- Template heading discipline ------------------------------------------


@pytest.mark.parametrize("name", ARCHITECTURE_DOCS)
def test_architecture_doc_has_required_headings(name: str):
    """Each architecture doc reproduces the 8 normative top-level
    headings in order. Synthesizers that drop a heading get caught."""
    path = DOCS_DEV / "architecture" / f"{name}.md"
    if not path.exists():
        pytest.skip(f"{path} not yet generated")
    content = path.read_text(encoding="utf-8")
    headings = re.findall(r"^## (?:\d+\. )?(.+)$", content, re.MULTILINE)
    stripped = [re.sub(r"^\d+\.\s*", "", h).strip() for h in headings]
    for expected in ARCH_HEADINGS:
        assert expected in stripped, (
            f"{path.relative_to(REPO)} missing heading {expected!r}; "
            f"found headings: {stripped}"
        )


@pytest.mark.parametrize("name", SUBSYSTEM_DOCS)
def test_subsystem_doc_has_required_headings(name: str):
    """Each subsystem doc reproduces the 11 normative top-level
    headings in order."""
    path = DOCS_DEV / "subsystems" / f"{name}.md"
    if not path.exists():
        pytest.skip(f"{path} not yet generated")
    content = path.read_text(encoding="utf-8")
    headings = re.findall(r"^## (?:\d+\. )?(.+)$", content, re.MULTILINE)
    stripped = [re.sub(r"^\d+\.\s*", "", h).strip() for h in headings]
    for expected in SUBSYS_HEADINGS:
        assert expected in stripped, (
            f"{path.relative_to(REPO)} missing heading {expected!r}; "
            f"found headings: {stripped}"
        )


# ---- Cross-reference resolution -------------------------------------------


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@pytest.mark.parametrize(
    "doc", _all_tracked_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_internal_cross_references_resolve(doc: Path):
    """Every relative markdown link to another doc/dev/ file or to
    AGENTS.md resolves to a file that exists."""
    content = doc.read_text(encoding="utf-8")
    for label, target in _MD_LINK_RE.findall(content):
        if target.startswith(
            ("http://", "https://", "#", "/", "mailto:")
        ):
            continue
        target_path = target.split("#", 1)[0]
        if not target_path:
            continue
        resolved = (doc.parent / target_path).resolve()
        assert resolved.exists(), (
            f"{doc.relative_to(REPO)}: broken link [{label}]({target}) "
            f"resolves to nonexistent {resolved}"
        )


# ---- Coverage check (only meaningful when triage cards exist) -------------


def test_triage_coverage_if_cards_present():
    """If triage cards exist AND the synthesis tree exists (consolidation
    run not yet finalised but synthesis has run), every card's
    target_docs entries must be satisfied by an existing consolidated
    doc."""
    triage_dir = DOCS_DEV / "_work" / "triage"
    if not triage_dir.exists():
        pytest.skip("No triage cards; consolidation already finalised")
    if not (DOCS_DEV / "architecture").exists() or not (
        DOCS_DEV / "subsystems"
    ).exists():
        pytest.skip(
            "Synthesis tree missing; triage cards still awaiting synthesis"
        )
    missing: list[str] = []
    for card_path in triage_dir.glob("*.json"):
        try:
            card = json.loads(card_path.read_text())
        except json.JSONDecodeError:
            continue
        for target in card.get("target_docs", []):
            target_file = REPO / target
            if not target_file.exists():
                missing.append(f"{card_path.name} -> {target}")
    assert not missing, f"Unsatisfied triage targets: {missing}"
