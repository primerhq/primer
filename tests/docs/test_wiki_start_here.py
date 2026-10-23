"""The start-here pages pinned against the live code (pivot P1, task 14).

The five pages the seeded agents read first are the agent docs for this
plan's tools, so their claims stay accurate by test: the five paths exist,
the core-kit page names exactly the core kit and links every core-kit
reference page, calling-tools shows a call_tool call and describe_tool, and
the errors page names every error type.
"""

from primer.knowledge.wiki.build import wiki_links
from primer.knowledge.wiki.reference import reference_path
from primer.toolset.core_kit import CORE_KIT_TOOLS
from tests.docs._wiki import repo_wiki

START_HERE = ["start-here/reading-this-wiki", "start-here/calling-tools", "start-here/roles-and-approvals",
              "start-here/errors", "start-here/the-core-kit"]


def test_start_here_has_its_five_pages():
    assert set(START_HERE) <= set(repo_wiki().pages)


def test_the_core_kit_page_names_exactly_the_core_kit():
    assert repo_wiki().pages["start-here/the-core-kit"].meta["tools"] == list(CORE_KIT_TOOLS)


def test_start_here_links_every_core_kit_reference_page():
    linked = {t for p in START_HERE for t in wiki_links(repo_wiki().pages[p].body)}
    assert {reference_path(*tid.split("__", 1)) for tid in CORE_KIT_TOOLS} <= linked


def test_calling_tools_shows_a_call_tool_call_and_describe_tool():
    body = repo_wiki().pages["start-here/calling-tools"].body
    assert '"toolset_id"' in body and '"tool_name"' in body and "describe_tool" in body


def test_the_errors_page_names_every_error_type():
    body = repo_wiki().pages["start-here/errors"].body
    assert all(f"`{t}`" in body for t in ("validation-error", "not-found", "forbidden", "approval-required",
                                         "provider-error", "tool-call-error", "conflict", "bad-request",
                                         "unsupported-surface", "tool-error", "sign-in-required", "unavailable"))
