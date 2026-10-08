"""Journey: a link to a workspace that does not exist says so, instead of a half-working shell with false empty states (console review 2026-10-08, C-019).

``#/w/nope`` (a deleted or mistyped workspace id) kept the previous page's session tab, headed the Files sidebar ``FILES nope`` and told the user
"This workspace has no files yet" although the files request was a 404, and fired seven 404s (tree, log, tap, attach, yields) with no message naming the
cause. The Studio now asks for the workspace row; a 404 shows a not-found card with a way to a real workspace, and what polled the missing workspace
is no longer mounted, so the requests stop.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_shell

MISSING = "nope-c019"


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


@pytest.mark.ui_e2e
def test_a_link_to_an_unknown_workspace_says_so_and_offers_the_real_ones(page: Page, base_url: str, console_url: str) -> None:
    real = _a_workspace_id(base_url)
    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url))

    # The earlier page's session tab is carried in the link, the way a stale tab is.
    page.goto(f"{console_url}#/w/{MISSING}?doc=session:sess-carried-c019")

    gone = page.get_by_test_id("nv-ws-gone")
    expect(gone).to_be_visible(timeout=20_000)
    expect(gone).to_contain_text(f"'{MISSING}' was not found")
    expect(page.get_by_test_id("nv-files-panel")).to_have_count(0)
    expect(page.get_by_test_id("nv-tg-tab:session:sess-carried-c019")).to_have_count(0)
    expect(page.get_by_text("This workspace has no files yet")).to_have_count(0)

    # The first render asks optimistically (a valid link is the common case, so nothing waits on the workspace row). Once the card is up, the
    # missing workspace and the carried-over session are not asked for again: nothing that polls them is still mounted.
    assert [u for u in requests if f"/workspaces/{MISSING}" in u and u.split("?")[0].endswith(f"/workspaces/{MISSING}")], "the workspace row was never asked for"
    page.wait_for_timeout(500)
    seen = [u for u in requests if MISSING in u or "sess-carried-c019" in u]
    page.wait_for_timeout(7_000)
    later = [u for u in requests if MISSING in u or "sess-carried-c019" in u][len(seen):]
    assert not later, f"still asking about the missing workspace after the card: {later}"

    gone.get_by_test_id(f"nv-ws-gone-open:{real}").click()
    expect(gone).to_have_count(0, timeout=10_000)
    expect(page.get_by_test_id("nv-files-panel")).to_be_visible(timeout=15_000)
    assert f"#/w/{real}" in page.url


@pytest.mark.ui_e2e
def test_a_real_workspace_does_not_show_the_card(page: Page, base_url: str, console_url: str) -> None:
    real = _a_workspace_id(base_url)
    open_shell(page, console_url, real)
    expect(page.get_by_test_id("nv-files-panel")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(1_000)
    expect(page.get_by_test_id("nv-ws-gone")).to_have_count(0)
