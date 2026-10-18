"""An old link to the retired search toolset says where search went (plan 06 Task 4, Review Focus 2).

The legacy route ``toolsets/search`` resolves to the toolset detail overlay; the
detail answers 404, and the console must say "Toolset not found" with the reason,
never the server's raw "Not Found:" title. Lead-scheduled (ui_e2e); the unit
pins are ``tests/ui/test_toolset_not_found.py``.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_legacy_route

SEARCH_REASON = ("There is no search toolset. Agents search this install's agents, graphs, collections and tools with the "
                 "Collections toolset's search tool, by meaning once internal search is on in System \u203a Internal collections.")


def test_an_old_link_to_the_search_toolset_says_not_found(page: Page, console_url: str) -> None:
    body = open_legacy_route(page, console_url, "toolsets/search")
    expect(page.get_by_test_id("toolset-not-found-reason")).to_have_text(SEARCH_REASON, timeout=15_000)
    expect(body).not_to_contain_text("Not Found:")
