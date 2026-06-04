"""Tests for the YAML-frontmatter parser used by the user_docs service."""

from __future__ import annotations

import pytest

from primer.user_docs_service import parse_frontmatter, FrontmatterError


def test_parses_basic_frontmatter():
    src = "---\nslug: agents\ntitle: Agents\n---\nbody here\n"
    fm, body = parse_frontmatter(src)
    assert fm == {"slug": "agents", "title": "Agents"}
    assert body == "body here\n"


def test_returns_empty_frontmatter_and_full_body_when_absent():
    src = "no frontmatter here\nbody text\n"
    fm, body = parse_frontmatter(src)
    assert fm == {}
    assert body == src


def test_parses_list_values():
    src = "---\ntags: [a, b, c]\nrelated: []\n---\nx\n"
    fm, _ = parse_frontmatter(src)
    assert fm["tags"] == ["a", "b", "c"]
    assert fm["related"] == []


def test_parses_nested_yaml_block():
    src = "---\nslug: agents\nheadings:\n  - Overview\n  - Lifecycle\n---\nx\n"
    fm, _ = parse_frontmatter(src)
    assert fm["headings"] == ["Overview", "Lifecycle"]


def test_unclosed_frontmatter_raises():
    src = "---\nslug: agents\nbody never reaches it\n"
    with pytest.raises(FrontmatterError) as exc_info:
        parse_frontmatter(src)
    assert "unclosed" in str(exc_info.value).lower()


def test_invalid_yaml_inside_frontmatter_raises():
    src = "---\nslug: ag: ents\n  invalid:: yaml\n---\nx\n"
    with pytest.raises(FrontmatterError):
        parse_frontmatter(src)
