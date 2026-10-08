"""Journey: a link to a session that does not exist says so, instead of opening a live-looking empty tab (console review 2026-10-08, C-020).

``#/w/<wid>?doc=session:sess-bogus`` (a mistyped link, or a session deleted elsewhere whose tab is restored) used to open a tab with a
binding chip, a "Waiting" status, an empty transcript and a composer that accepted input, while the session row, the history and the pending
yields all answered 404 underneath. The document now treats a 404 on the session row as a state of its own: it says the session no longer
exists, offers to close the tab, has no composer, and stops asking the server about it.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_doc

BOGUS = "sess-bogus-c020"


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


@pytest.mark.ui_e2e
def test_a_link_to_a_missing_session_says_it_no_longer_exists_and_the_tab_can_be_closed(
    page: Page, base_url: str, console_url: str,
) -> None:
    wid = _a_workspace_id(base_url)
    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url))

    open_doc(page, console_url, wid, "session", BOGUS)

    gone = page.get_by_test_id("nv-session-gone")
    expect(gone).to_be_visible(timeout=15_000)
    expect(gone).to_contain_text("no longer exists")
    expect(gone).to_contain_text(BOGUS)
    expect(page.get_by_test_id("nv-composer")).to_have_count(0)
    expect(page.get_by_test_id("nv-session-head")).to_have_count(0)

    assert not [u for u in requests if "/workspaces/null/" in u or "/workspaces/undefined/" in u], "a URL was built from an unresolved workspace id"

    # The document stops asking: after the first round of 404s nothing polls the missing session any more.
    page.wait_for_timeout(500)
    seen = len([u for u in requests if BOGUS in u])
    page.wait_for_timeout(7_000)
    assert len([u for u in requests if BOGUS in u]) == seen, "the missing session is still being polled"

    page.get_by_test_id("nv-session-gone-close").click()
    expect(page.get_by_test_id(f"nv-tg-tab:session:{BOGUS}")).to_have_count(0, timeout=10_000)
    expect(gone).to_have_count(0)


@pytest.mark.ui_e2e
def test_the_phone_shows_the_same_card_behind_its_back_button(page: Page, base_url: str, console_url: str) -> None:
    wid = _a_workspace_id(base_url)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{console_url}#/w/{wid}?doc=session:{BOGUS}")

    screen = page.get_by_test_id("nv-mob-session-screen")
    expect(screen).to_be_visible(timeout=20_000)
    expect(screen.get_by_test_id("nv-session-gone")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("nv-composer")).to_have_count(0)

    screen.get_by_test_id("nv-session-gone-close").click()
    expect(screen).to_have_count(0, timeout=10_000)
