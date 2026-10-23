"""The coverage tests over the platform wiki sources (pivot P1, task 13).

They read the build ``tests/docs/_wiki.py`` produces from the repo's checked-in
``docs/wiki`` sources and the live tool catalog, and pin the content contract
the feature pages (pivot P4) write to:

* one generated reference page per registered tool, and no page claims a tool's
  path twice;
* every call example in a json block validates against the live schemas (a
  ``call_tool`` wrapper against ``call_tool``'s schema and the inner tool's
  arguments against the inner tool's schema, a direct call against the named
  tool's schema);
* every wiki link resolves;
* no page names a tool or toolset that does not exist (the inline scoped ids,
  the JSON ``toolset_id`` values and the frontmatter ``tools``);
* the page template parses and carries the six page-format sections in order;
* no wiki page carries an em dash.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import jsonschema
import pytest

from primer.knowledge.wiki.build import unresolved_links, wiki_links
from primer.knowledge.wiki.pages import WikiPage, parse_page_source
from primer.knowledge.wiki.reference import reference_path
from tests.docs._wiki import WIKI_DIR, repo_wiki, tool_entries
from tests.docs._wiki_specs import PLANNED_PATHS

_SCOPED_ID_RE = re.compile(r"^(?P<head>[a-z_]+)(?P<sep>::|__)(?P<tail>[a-z0-9_]+)$")
_FENCED_RE = re.compile(r"^[ \t]{0,3}(`{3,})([^\n]*)\n(.*?)^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

_CALL_KEYS = {"toolset_id", "tool_name", "arguments"}
_DIRECT_KEYS = {"name", "arguments"}


def _pages(*kinds): return {p: v for p, v in repo_wiki().pages.items() if v.kind in kinds}


def _fenced_blocks(body: str) -> list[tuple[str, str]]:
    """(info string, content) for every fenced block of ``body``."""
    return [(match.group(2).strip(), match.group(3)) for match in _FENCED_RE.finditer(body)]


def _json_objects(block: str) -> list[dict[str, Any]]:
    """Every object of the block's parsed JSON, nested ones included."""
    try:
        value: Any = json.loads(block)
    except json.JSONDecodeError:
        return []
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            found.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _json_blocks_of(page: WikiPage) -> Iterator[str]:
    for info, block in _fenced_blocks(page.body):
        if info.lower().startswith("json"):
            yield block


def _inline_code_spans(body: str) -> list[str]:
    no_fences = _FENCED_RE.sub(" ", body)
    return [span.strip() for span in _INLINE_CODE_RE.findall(no_fences)]


def _entry_schemas() -> dict[str, dict[str, Any]]:
    return {entry.scoped_id: entry.tool.args_schema for entry in tool_entries()}


def _call_objects() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(page path, kind, object) for every call example of a json block.

    Kind ``call`` is an object that carries the three ``call_tool`` keys as a
    string toolset id, a string tool name and an argument object; kind
    ``direct`` is an object whose ``name`` is a scoped id and whose
    ``arguments`` is an object.
    """
    for path, page in sorted(repo_wiki().pages.items()):
        if page.kind not in ("guide", "section", "toc", "reference"):
            continue
        for block in _json_blocks_of(page):
            for obj in _json_objects(block):
                if (
                    _CALL_KEYS <= set(obj)
                    and isinstance(obj["toolset_id"], str)
                    and isinstance(obj["tool_name"], str)
                    and isinstance(obj["arguments"], dict)
                ):
                    yield path, "call", obj
                elif (
                    _DIRECT_KEYS <= set(obj)
                    and isinstance(obj["name"], str)
                    and _SCOPED_ID_RE.fullmatch(obj["name"]) is not None
                    and isinstance(obj["arguments"], dict)
                ):
                    yield path, "direct", obj


def _invalid_calls() -> list[str]:
    """One line per call example that does not validate against the live schemas."""
    schemas = _entry_schemas()
    invalid: list[str] = []
    for path, kind, obj in _call_objects():
        if kind == "call":
            try:
                jsonschema.validate(obj, schemas["system__call_tool"])
            except jsonschema.ValidationError as exc:
                invalid.append(f"{path}: the call_tool wrapper does not validate: {exc.message}")
                continue
            inner_id = f"{obj['toolset_id']}__{obj['tool_name']}"
            inner = schemas.get(inner_id)
            if inner is None:
                invalid.append(f"{path}: call_tool names the unknown tool {inner_id}")
                continue
            try:
                jsonschema.validate(obj["arguments"], inner)
            except jsonschema.ValidationError as exc:
                invalid.append(f"{path}: the arguments for {inner_id} do not validate: {exc.message}")
        else:
            schema = schemas.get(obj["name"])
            if schema is None:
                invalid.append(f"{path}: the direct call names the unknown tool {obj['name']}")
                continue
            try:
                jsonschema.validate(obj["arguments"], schema)
            except jsonschema.ValidationError as exc:
                invalid.append(f"{path}: the arguments for {obj['name']} do not validate: {exc.message}")
    return invalid


def _call_count() -> int:
    return sum(1 for _ in _call_objects())


@lru_cache(maxsize=1)
def _known_toolsets() -> frozenset[str]:
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS, RESERVED_TOOLSET_SCOPE_IDS

    return RESERVED_TOOLSET_IDS | RESERVED_TOOLSET_SCOPE_IDS | {"client"}


def _registered_ids() -> frozenset[str]:
    """The ids a tool answers to: its registered id and its catalog scoped id.

    A harness tool registers under an already-prefixed id (``harness__list``),
    so its scoped id is the prefix twice and the docs' spelling is the
    registered id itself; the set of both is what a page may carry.
    """
    return frozenset(
        {entry.tool.id for entry in tool_entries()} | {entry.scoped_id for entry in tool_entries()}
    )


def _known_scoped_id(name: str) -> bool:
    """True when a page may carry ``name``: the toolset exists and the tool answers to it."""
    match = _SCOPED_ID_RE.fullmatch(name)
    if match is None:
        return False
    head, tail = match.group("head"), match.group("tail")
    if head not in _known_toolsets():
        return False
    registered = _registered_ids()
    return tail in registered or f"{head}__{tail}" in registered


def _unknown_names() -> list[str]:
    """The names a page carries that no tool, toolset or scope answers to."""
    known_toolsets = _known_toolsets()
    names: set[str] = set()
    for page in repo_wiki().pages.values():
        for span in _inline_code_spans(page.body):
            if _SCOPED_ID_RE.fullmatch(span) is not None:
                names.add(span)
        names.update(page.meta.get("tools", ()))
    # only a call example's toolset_id names a toolset: an argument dict may
    # carry a "toolset_id" that is data (a python toolset being created), not
    # a reference
    toolset_ids = {obj["toolset_id"] for _path, kind, obj in _call_objects() if kind == "call"}
    unknown = [name for name in sorted(names) if not _known_scoped_id(name)]
    unknown += [toolset_id for toolset_id in sorted(toolset_ids) if toolset_id not in known_toolsets]
    return unknown


def test_every_registered_tool_has_exactly_one_reference_page():
    paths = [reference_path(e.toolset_id, e.tool.id) for e in tool_entries()]
    assert len(paths) == len(set(paths))
    tool_pages = {p for p, v in _pages("reference").items() if p.count("/") == 3}
    assert tool_pages == set(paths)


@pytest.mark.xfail(strict=False, reason="pivot P4 Task 23 removes this marker")
def test_every_reference_page_is_linked_from_a_feature_page():
    linked = {t for v in _pages("guide", "section").values() for t in wiki_links(v.body)}
    every = sorted(reference_path(e.toolset_id, e.tool.id) for e in tool_entries())
    assert [p for p in every if p not in linked] == []


def test_every_call_tool_example_validates_against_the_live_schema():
    # every ```json block of every guide, section, toc and reference page; every object in it (nested too) with
    # keys {"toolset_id", "tool_name", "arguments"} is validated against call_tool's args_schema AND the inner tool's
    # args_schema; every object with keys {"name", "arguments"} whose name is a scoped id against that tool's schema.
    assert _invalid_calls() == [] and _call_count() >= 1


def test_every_wiki_link_resolves():
    # a target that is a planned page not written yet (PLANNED_PATHS) resolves as soon as the page lands; Task 23 drops the allowance
    assert [x for x in unresolved_links(repo_wiki().pages) if x[1] not in PLANNED_PATHS] == []


def test_no_page_names_a_tool_or_toolset_that_does_not_exist():
    # inline code spans matching ^[a-z_]+(::|__)[a-z0-9_]+$, JSON "toolset_id" values and frontmatter "tools"
    assert _unknown_names() == []


def test_the_template_parses_as_a_page():
    page = parse_page_source((WIKI_DIR / "_template.md").read_text(), path="template")
    assert [h for h in page.body.splitlines() if h.startswith("## ")][:6] == [
        "## What it is", "## Concepts", "## How to", "## Tool calls", "## Gotchas", "## Related"]


def test_the_wiki_has_no_em_dash():
    assert [p for p in WIKI_DIR.rglob("*.md") if "\u2014" in p.read_text()] == []
