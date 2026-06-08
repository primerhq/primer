"""Validate the agent docs: frontmatter, no em-dash, link integrity.

The agent docs (docs/agents/**/*.md, excluding _-prefixed) are the
LLM-facing knowledge base. This guards their structural quality so the
polish does not rot.
"""
from __future__ import annotations

import re

import pytest

from primer.ai_docs_path import resolve_ai_docs_dir

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _docs():
    root = resolve_ai_docs_dir()
    return [
        p for p in sorted(root.rglob("*.md")) if not p.name.startswith("_")
    ], root


def _frontmatter(text: str) -> dict[str, str]:
    m = _FM.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-", "#")):
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def test_agent_docs_have_required_frontmatter():
    docs, root = _docs()
    assert docs, "no agent docs found"
    for p in docs:
        fm = _frontmatter(p.read_text(encoding="utf-8"))
        rel = p.relative_to(root).as_posix()
        for key in ("slug", "title", "summary"):
            assert fm.get(key), f"{rel}: missing frontmatter '{key}'"


def test_agent_docs_have_no_em_dash():
    docs, root = _docs()
    for p in docs:
        text = p.read_text(encoding="utf-8")
        assert "\u2014" not in text, (
            f"{p.relative_to(root).as_posix()}: contains em-dash (U+2014)"
        )


def test_agent_doc_related_links_resolve():
    docs, root = _docs()
    slugs = {p.relative_to(root).with_suffix("").as_posix() for p in docs}
    # also accept bare stems for top-level docs
    slugs |= {p.stem for p in docs}
    for p in docs:
        fm_block = _FM.match(p.read_text(encoding="utf-8"))
        if not fm_block:
            continue
        m = re.search(r"related:\s*\[(.*?)\]", fm_block.group(1))
        if not m:
            continue
        for ref in (s.strip() for s in m.group(1).split(",") if s.strip()):
            assert ref in slugs, (
                f"{p.relative_to(root).as_posix()}: related '{ref}' not found"
            )
