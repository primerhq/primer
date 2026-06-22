"""Hygiene tests for the agent-facing docs under docs/agents/.

The dev-doc tier (docs/dev/ + AGENTS.md) is guarded by
``test_docs_hygiene.py``. The agent-facing tier (docs/agents/) is the
knowledge base ingested into the reserved ``_internal_ai_docs``
collection and reached by agents via ``search::search_ai_docs``; it had
no equivalent guard, which let a stale "no mid-graph pause in v1" claim
drift past the shipped graph human-in-the-loop feature. This module
mirrors the dev guard for the agent tier so those docs cannot silently
rot: no em dashes, every relative link resolves, and every doc carries
the agent-doc frontmatter and required headings.

The ingest contract (``docs/agents/_README.md``): the bootstrap walk
``rglob("*.md")`` and skips files whose name starts with ``_``. These
tests apply the same skip rule so the contract and its guard stay in
lockstep.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS_AGENTS = REPO / "docs" / "agents"

EM_DASH = "—"

# Required top-level headings, per the agent-doc template recorded in
# docs/agents/_README.md. A doc may interleave extra headings; these must
# be present. We require only the headings every reference doc has: most
# carry "MCP tools" + "Workflows" too, but chats.md drives over REST/
# console rather than MCP and titles that section differently, so those
# two are intentionally not universal. Cookbook recipes follow a
# different template and are checked against COOKBOOK_HEADINGS below.
DOC_HEADINGS = [
    "Overview",
    "Mental model",
    "Gotchas",
    "Related",
]

COOKBOOK_HEADINGS = [
    "Goal",
    "Prerequisites",
    "Steps",
    "Verify",
    "Gotchas",
    "Related",
]

# Frontmatter keys every ingested agent doc carries. ``related`` is
# present on the top-level reference docs but not on cookbook recipes, so
# it is not required here.
REQUIRED_FRONTMATTER_KEYS = ["slug", "title", "summary"]

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _ingested_docs() -> list[Path]:
    """Every markdown file the bootstrap walk would ingest.

    Mirrors the ingest contract: recursive ``*.md`` minus files whose
    name starts with ``_`` (e.g. ``_README.md``). Empty list if the
    directory does not exist so the module imports cleanly anywhere.
    """
    if not DOCS_AGENTS.exists():
        return []
    return sorted(
        p for p in DOCS_AGENTS.rglob("*.md") if not p.name.startswith("_")
    )


def _is_cookbook(doc: Path) -> bool:
    return "cookbook" in doc.relative_to(DOCS_AGENTS).parts


def _strip_code(content: str) -> str:
    """Remove fenced and inline code so code syntax is not mistaken for
    a markdown link or an em dash in prose."""
    without_fences = _FENCED_CODE_RE.sub("", content)
    return _INLINE_CODE_RE.sub("", without_fences)


# ---- Discoverability -------------------------------------------------------


def test_agent_docs_present():
    """The directory exists and ingests at least the core reference set.

    This catches an accidental relocation of the whole tier (the same
    class of breakage that dead ``primer/ai_docs`` references describe).
    """
    docs = _ingested_docs()
    assert docs, f"No ingestable agent docs found under {DOCS_AGENTS}"
    names = {p.stem for p in docs if not _is_cookbook(p)}
    for core in ("agents", "sessions", "graphs", "yielding", "channels"):
        assert core in names, f"core agent doc {core!r} missing from {names}"


# ---- Em-dash absence -------------------------------------------------------


@pytest.mark.parametrize(
    "doc", _ingested_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_no_em_dashes(doc: Path):
    """No em dash characters anywhere in an ingested agent doc."""
    content = doc.read_text(encoding="utf-8")
    if EM_DASH in content:
        offending = [
            f"  line {i + 1}: {line}"
            for i, line in enumerate(content.splitlines())
            if EM_DASH in line
        ]
        pytest.fail(
            f"{doc.relative_to(REPO)} contains em dashes:\n"
            + "\n".join(offending)
        )


# ---- Frontmatter -----------------------------------------------------------


@pytest.mark.parametrize(
    "doc", _ingested_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_frontmatter_present(doc: Path):
    """Every ingested doc opens with a ``---`` frontmatter block carrying
    at least slug/title/summary, the contract the ingester and the docs
    site both rely on."""
    content = doc.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    assert match, (
        f"{doc.relative_to(REPO)} is missing the leading --- frontmatter "
        "block"
    )
    block = match.group(1)
    keys = set(re.findall(r"^([a-z_]+):", block, re.MULTILINE))
    missing = [k for k in REQUIRED_FRONTMATTER_KEYS if k not in keys]
    assert not missing, (
        f"{doc.relative_to(REPO)} frontmatter missing keys: {missing} "
        f"(found {sorted(keys)})"
    )


# ---- Required headings -----------------------------------------------------


@pytest.mark.parametrize(
    "doc", _ingested_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_required_headings_present(doc: Path):
    """Each doc carries the required top-level headings for its template
    (reference docs vs cookbook recipes). Extra headings are allowed; a
    dropped required one is caught."""
    content = doc.read_text(encoding="utf-8")
    headings = {h.strip() for h in _HEADING_RE.findall(content)}
    expected = COOKBOOK_HEADINGS if _is_cookbook(doc) else DOC_HEADINGS
    missing = [h for h in expected if h not in headings]
    assert not missing, (
        f"{doc.relative_to(REPO)} missing headings {missing}; "
        f"found {sorted(headings)}"
    )


# ---- Cross-reference resolution -------------------------------------------


@pytest.mark.parametrize(
    "doc", _ingested_docs(), ids=lambda p: str(p.relative_to(REPO))
)
def test_internal_links_resolve(doc: Path):
    """Every relative markdown link (to another agent doc or any file in
    the repo) resolves to a file that exists. External, anchor, absolute,
    and mailto links are skipped, as is link-like syntax inside code."""
    content = _strip_code(doc.read_text(encoding="utf-8"))
    for label, target in _MD_LINK_RE.findall(content):
        if target.startswith(("http://", "https://", "#", "/", "mailto:")):
            continue
        target_path = target.split("#", 1)[0]
        if not target_path:
            continue
        resolved = (doc.parent / target_path).resolve()
        assert resolved.exists(), (
            f"{doc.relative_to(REPO)}: broken link [{label}]({target}) "
            f"resolves to nonexistent {resolved}"
        )
