"""W5 (wiki-first): the collaboration section and the wiki-driven agent recipe.

The generic page tests (``test_wiki_pages.py``) and the coverage tests
(``test_wiki_coverage.py``) pin the shared contract every page holds:
frontmatter, template order, link resolution, worked calls against the
live schemas and the refusal of unknown tool names. These tests pin what
W5 itself must hold on disk: the boot build includes the new pages, the
exact scoped ids and reference links the pages document, the OQ-1 file
shape the coordinator pattern promises, the open-scope statement, and the
purpose-map rows that route the two new goals.
"""

from tests.docs._wiki import repo_wiki

PAGES = repo_wiki().pages


def test_the_boot_build_includes_the_w5_pages():
    kinds = {p: v.kind for p, v in PAGES.items()}
    assert kinds["collaboration"] == "section"
    assert kinds["collaboration/coordinator-pattern"] == "guide"
    assert kinds["collaboration/other-patterns"] == "guide"
    assert kinds["agents/wiki-driven-agent"] == "guide"


def test_the_coordinator_page_documents_the_pinned_file_watch_surface():
    page = PAGES["collaboration/coordinator-pattern"]
    assert page.meta["tools"] == [
        "workspace__ls", "workspace__read", "workspace__write", "workspace_ext__watch_files",
    ]
    for ref in (
        "reference/tools/workspace_ext/watch_files",
        "reference/tools/workspace/write",
        "reference/tools/workspace/read",
        "reference/tools/workspace/ls",
    ):
        assert f"]({ref})" in page.body, f"{ref}: no link to the reference page"


def test_the_coordinator_page_promises_the_oq1_file_shape():
    body = PAGES["collaboration/coordinator-pattern"].body
    # the spec default: one task-assignment file plus one response file per sub-agent
    assert "ONE response file per sub-agent" in body
    assert "<agent-id>.status.md" in body
    # the shared-response variant stays documented as the trade-off, not dropped
    assert "ONE shared response file" in body
    # the two semantics the spec pins: the re-call after every timeout and the parked state
    assert "re-calls" in body and "parked_on" in body


def test_the_other_patterns_page_pins_the_evidence_backed_surfaces():
    page = PAGES["collaboration/other-patterns"]
    assert page.meta["tools"] == [
        "workspaces__message_session", "system__wait_for_event",
        "trigger__create_subscription", "trigger__fire_now",
        "system__invoke_agent", "workspace_ext__invoke_graph",
    ]
    for ref in (
        "reference/tools/workspaces/message_session",
        "reference/tools/system/wait_for_event",
        "reference/tools/trigger/create_subscription",
        "reference/tools/trigger/fire_now",
        "reference/tools/system/invoke_agent",
        "reference/tools/workspace_ext/invoke_graph",
    ):
        assert f"]({ref})" in page.body, f"{ref}: no link to the reference page"
    # the open scope is stated, not implied
    assert "scope of this page is open" in page.body


def test_the_other_patterns_page_keeps_the_approval_surfaces_apart():
    body = PAGES["collaboration/other-patterns"].body
    # 01a1bcf3: the two delegation surfaces keep distinct refusal semantics.
    # Graph side: a rejected or timed-out approval fails the child graph and
    # the caller's answer is an error result. Sub-agent side: the refusal lands
    # on the sub-agent's parked call and its turn may continue, so the outer
    # invoke_agent can still return final text. The old conflation stays gone.
    assert "fails the child graph" in body
    assert "error result of the sub-agent's parked call" in body
    assert "final text successfully" in body
    assert "refuses the whole invocation" not in body


def test_the_wiki_driven_recipe_teaches_the_loop_and_pins_the_probe():
    body = PAGES["agents/wiki-driven-agent"].body
    # the loop in its documented order: index, search, read, asserted on the grounding
    # prompt itself (the grant list above it carries the ids in a different order)
    prompt = body.index('"system_prompt"')
    loop = body[prompt:]
    assert loop.index("purpose-map") < loop.index("wiki_search") < loop.index("wiki_read")
    # the characterization probe asserts observed behavior: the documented path, no raw-API probing
    assert "characterization probe" in body and "raw REST API" in body
    # the live real-LLM observation is CI-owned; the executable artifact stays in the page
    assert "CI-owned" in body


def test_the_purpose_map_routes_the_w5_goals():
    body = PAGES["purpose-map"].body
    assert "](collaboration/coordinator-pattern)" in body
    assert "](collaboration/other-patterns)" in body
    assert "](agents/wiki-driven-agent)" in body
