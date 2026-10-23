"""The page tests over the platform wiki sources (pivot P4, task 1).

Every page the wiki will hold is defined by a :class:`PageSpec`
(``tests/docs/_wiki_specs.py``, merged from the modules of
``tests/docs/wiki_specs/``): the planned path, the title and summary the
frontmatter must carry, the audience, the order, the scoped tool ids the page
must document and show, the related paths, the strings the body must say, and
the length cap. The tests here check the helper module itself (the first
group), every page against its spec (the parametrised group), and the wiki as
a whole (the retired notation, the credential scan and the drift guards).

While ``STRICT`` is False, a spec whose page is not written yet skips its
page tests (``_page``); the drift guards skip with their pages. Task 23 sets
``STRICT`` True and drops the ``PLANNED_PATHS`` allowance from the link tests,
so every planned path must hold a page by then.
"""

from __future__ import annotations

import re

import pytest

from primer.knowledge.wiki.pages import parse_page_source
from primer.knowledge.wiki.reference import reference_path
from tests.docs._wiki import WIKI_DIR, repo_wiki, tool_entries
from tests.docs._wiki_specs import (
    DRIFT_GUARDS,
    PLANNED_PATHS,
    STRICT,
    PageSpec,
    all_specs,
    calls_in_body,
    credential_hits,
    expected_tools,
    order_violations,
    render_frontmatter,
    scoped,
)

SPECS = all_specs()
PAGES = repo_wiki().pages


def _page(path: str):
    """The built page at ``path``; a skip while ``STRICT`` is False, a failure when it is True."""
    page = PAGES.get(path)
    if page is None:
        if not STRICT:
            pytest.skip(f"{path}: not written yet (STRICT is False)")
        raise KeyError(path)
    return page


def _headings(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("## ")]


def _subsequence(available: list[str], wanted: list[str]) -> bool:
    """True when every wanted heading appears in ``available``, in order (extras allowed)."""
    rest = available
    for want in wanted:
        if want not in rest:
            return False
        rest = rest[rest.index(want) + 1:]
    return True


# ---- the helper module itself -----------------------------------------------------------------------------------


def test_scoped_builds_ids():
    assert scoped("crud", "create_agent update_agent") == ("crud__create_agent", "crud__update_agent")


@pytest.mark.parametrize("bad", ['say "hi"', "a " + chr(0x2014) + " b", "x" * 161, "two\nlines"])
def test_a_bad_summary_is_refused(bad):
    with pytest.raises(ValueError):
        PageSpec("a/b", "T", bad, "feature", 1)


def test_expected_tools_adds_a_conditional_tool_only_when_it_exists():
    spec = PageSpec("a/b", "T", "S", "feature", 1, tools=("misc__uuid_v4",), tools_if_present=("misc__nope", "system__call_tool"))
    assert expected_tools(spec) == ["misc__uuid_v4", "system__call_tool"]


def test_calls_in_body_reads_both_shapes_and_skips_other_json():
    body = ('```json\n{"toolset_id": "harness", "tool_name": "harness__list", "arguments": {}}\n```\n'
            '```json\n{"name": "workspace__read", "arguments": {"path": "a"}}\n```\n```json\n{"id": "a"}\n```\n```json\n{oops}\n```\n')
    assert calls_in_body(body) == ["harness__harness__list", "workspace__read"]


def test_render_frontmatter_round_trips_through_the_page_parser():
    spec = PageSpec("agents/x", "Why: the title", "Admin: a summary, with a colon.", "feature", 3, audience="admin",
                    tools=scoped("misc", "uuid_v4"), related=("agents/y",))
    page = parse_page_source(render_frontmatter(spec) + "## What it is\nx\n", path=spec.path)
    assert (page.title, page.summary) == (spec.title, spec.summary)
    assert page.meta["tools"] == ["misc__uuid_v4"] and page.meta["related"] == ["agents/y"]
    assert (page.meta["audience"], page.meta["order"]) == ("admin", 3)


def test_the_order_rule_flags_a_session_started_before_its_workspace():
    body = "".join(f'```json\n{{"toolset_id": "workspaces", "tool_name": "{t}", "arguments": {{}}}}\n```\n'
                   for t in ("create_workspace_session", "create_workspace"))
    assert order_violations(body) == [("workspaces__create_workspace", "workspaces__create_workspace_session")]


def test_the_credential_scan_flags_each_token_shape_and_not_a_placeholder():
    for bad in ("xoxb-123456789012-abc", "ghp_" + "a" * 36, "sk-" + "a" * 30, "123456789:" + "A" * 35, "AKIA" + "A" * 16):
        assert credential_hits(bad), bad
    assert not credential_hits("xoxb-REPLACE and <slack-bot-token> and xapp-REPLACE")


def test_the_recipe_template_parses_as_a_page():
    page = parse_page_source((WIKI_DIR / "_template-recipe.md").read_text(), path="template-recipe")
    assert [h for h in page.body.splitlines() if h.startswith("## ")] == [
        "## Goal", "## Prerequisites", "## Steps", "## Verify", "## Gotchas", "## Related"]


def test_planned_paths_are_slugs_and_cover_every_spec():
    assert all(re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)*", p) for p in PLANNED_PATHS)
    assert set(all_specs()) <= PLANNED_PATHS and len(PLANNED_PATHS) == 100


# ---- every page against its spec ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(SPECS))
def test_the_page_exists_with_exactly_the_planned_frontmatter(path):
    spec, page = SPECS[path], PAGES[path]            # KeyError: the page is missing
    assert page.kind == ("section" if spec.kind == "section" else "guide")
    assert (page.title, page.summary) == (spec.title, spec.summary)
    assert page.meta.get("audience", "user") == spec.audience and page.meta.get("order") == spec.order
    assert page.meta.get("tools", []) == expected_tools(spec) and page.meta.get("related", []) == list(spec.related)


@pytest.mark.parametrize("path", sorted(SPECS))
def test_every_tool_a_spec_lists_exists(path):
    live = {e.scoped_id for e in tool_entries()}
    assert [t for t in SPECS[path].tools if t not in live] == []


@pytest.mark.parametrize("path", sorted(SPECS))
def test_a_page_follows_its_template(path):
    spec, page = SPECS[path], _page(path)
    headings = _headings(page.body)
    if spec.kind == "feature":
        wanted = ["## What it is", "## Concepts"]
        if expected_tools(spec):
            wanted += ["## How to", "## Tool calls"]
        wanted += ["## Gotchas", "## Related"]
        assert headings and headings[0] == "## What it is" and headings[-1] == "## Related", headings
        assert _subsequence(headings, wanted), f"{path}: {headings} is not in the template order {wanted}"
    elif spec.kind == "recipe":
        wanted = ["## Goal", "## Prerequisites", "## Steps", "## Verify", "## Gotchas", "## Related"]
        assert headings and headings[0] == "## Goal" and headings[-1] == "## Related", headings
        assert _subsequence(headings, wanted), f"{path}: {headings} is not in the recipe order {wanted}"
        lines = page.body.splitlines()
        start = lines.index("## Steps")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        assert any(line.startswith("### ") for line in lines[start + 1:end]), f"{path}: Steps has no numbered sub-steps"
    else:  # section: the intro is the body after the "# <title>" line and before "## Pages"
        lines = page.body.splitlines()
        end = next((i for i in range(1, len(lines)) if lines[i].startswith("## ")), len(lines))
        intro = "\n".join(lines[1:end]).strip()
        assert len(intro) >= 80, f"{path}: the section intro is {len(intro)} characters (at least 80 are required)"


@pytest.mark.parametrize("path", sorted(SPECS))
def test_a_feature_page_links_and_shows_every_tool_it_lists(path):
    spec = SPECS[path]
    if spec.kind != "feature":
        pytest.skip("feature specs only")
    page = _page(path)
    body, called = page.body, calls_in_body(page.body)
    for tool in expected_tools(spec):
        toolset, _, name = tool.partition("__")
        assert f"]({reference_path(toolset, name)})" in body, f"{tool}: no link to its reference page"
        assert tool in called, f"{tool}: no worked call"


@pytest.mark.parametrize("path", sorted(SPECS))
def test_a_recipe_calls_exactly_the_tools_it_lists_in_a_sound_order(path):
    spec = SPECS[path]
    if spec.kind != "recipe":
        pytest.skip("recipe specs only")
    page = _page(path)
    assert sorted(set(calls_in_body(page.body))) == sorted(expected_tools(spec))
    assert order_violations(page.body) == []


@pytest.mark.parametrize("path", sorted(SPECS))
def test_related_paths_resolve(path):
    assert [r for r in SPECS[path].related if r not in PAGES and r not in PLANNED_PATHS] == []   # Task 23 drops the PLANNED_PATHS clause


@pytest.mark.parametrize("path", sorted(SPECS))
def test_the_page_says_what_the_plan_requires(path):
    page = _page(path)
    assert [m for m in SPECS[path].must if m not in page.body] == []


@pytest.mark.parametrize("path", sorted(SPECS))
def test_a_page_stays_readable(path):
    page = _page(path)
    assert len(page.body) <= SPECS[path].max_chars


@pytest.mark.parametrize("path", sorted(SPECS))
def test_admin_tools_are_labelled(path):
    # a user-audience page that lists a tool whose required_role is admin must say "admin" (any case) in its body
    spec, page = SPECS[path], _page(path)
    if spec.audience != "user":
        return
    roles = {e.scoped_id: e.required_role for e in tool_entries()}
    admin_tools = [t for t in expected_tools(spec) if roles.get(t) == "admin"]
    if admin_tools:
        assert "admin" in page.body.lower(), f"{path}: lists the admin tools {admin_tools} without saying 'admin'"


# ---- the wiki as a whole -----------------------------------------------------------------------------------------

# The retired notation and names (the plan's lists): the "<toolset>::<tool>" spelling for the toolsets that use the
# scoped id today, and the retired underscore collection ids and search tool names.
_RETIRED_TOOLSETS = ("system", "workspaces", "workspace_ext", "workspace", "trigger", "harness", "crud",
                     "web", "misc", "collections", "client", "canvas", "search")
_RETIRED_TOOL_RE = re.compile(r"\b(" + "|".join(_RETIRED_TOOLSETS) + r")::([a-z][a-z0-9_]*)")
_RETIRED_NAMES = ("_internal_ai_docs", "_internal_agents", "_internal_graphs", "_internal_tools",
                  "_internal_collections", "search_ai_docs", "search_tools", "search_agents",
                  "search_graphs", "search_collections")


def _source_pages():
    return {path: page for path, page in PAGES.items() if page.kind in ("guide", "section", "toc")}


def test_the_retired_notation_and_names_are_gone():
    hits: list[str] = []
    for path, page in sorted(_source_pages().items()):
        body = page.body
        hits += [f"{path}: {toolset}::{name}" for toolset, name in _RETIRED_TOOL_RE.findall(body)]
        hits += [f"{path}: {name}" for name in _RETIRED_NAMES if re.search(rf"\b{re.escape(name)}\b", body)]
    assert hits == []


def test_no_example_holds_a_credential():
    assert [(p, hit) for p, page in sorted(_source_pages().items()) for hit in credential_hits(page.body)] == []


@pytest.mark.parametrize("guard", DRIFT_GUARDS, ids=lambda g: g.label)
def test_the_page_names_everything_the_code_defines(guard):
    body = _page(guard.page_path).body
    assert [n for n in guard.names() if n not in body] == []
