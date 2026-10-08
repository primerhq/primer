"""Journey: a link with junk in it is rewritten to what the console actually applied (console review C-025).

The shell reads the hash on every navigation and drops what it does not know (an overlay or view section outside the grammar, a doc of an unknown
kind, a path that is not ``/w/<id>``), but it only WRITES the address back when one of its state values changed. A junk part that parsed to
"nothing" changed nothing, so ``?overlay=bogus`` stayed in the address bar with no overlay, and a shared link carried a parameter that meant
nothing. The address bar now shows what was applied; a valid link is left byte-identical.

Hash changes are made the way ``_shell_helpers.open_overlay`` makes them: by assigning ``location.hash`` on the loaded console, which is what
queues the ``hashchange`` the shell listens for.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_shell

pytestmark = smk("SMK-UI-06", status="partial")


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return c.get("/v1/workspaces").json()["items"][0]["id"]


def _go(page: Page, fragment: str) -> None:
    page.evaluate("(h) => { window.location.hash = h; }", fragment)


def _hash(page: Page) -> str:
    return page.evaluate("() => window.location.hash")


def _settles(page: Page, predicate_js: str, what: str, *, timeout: int = 10_000) -> None:
    """Wait until ``predicate_js`` (a function of the hash) holds; on a timeout say what the address bar was instead."""
    try:
        page.wait_for_function(f"(h => ({predicate_js}))(window.location.hash)", timeout=timeout)
    except PlaywrightError:
        raise AssertionError(f"the address bar never became {what}; it is {_hash(page)!r}") from None


@pytest.mark.ui_e2e
@pytest.mark.parametrize(
    ("junk", "applied"),
    [
        # Junk that parses to the state the console is already in: no state value moves, so nothing used to rewrite the address bar.
        ("?overlay=bogus", ""),
        ("?foo=bar&overlay=bogus", ""),
        ("?view=bogus", ""),
        ("?view=studio:nope", ""),
        ("?doc=nonsense:x", ""),
        ("?doc=session:", ""),
        # Junk next to a real change: the write already happened, these pin that it stays correct.
        ("?view=platform:bogus", "?view=platform"),
        ("?doc=nonsense:x&overlay=agents", "?overlay=agents"),
        ("?doc=session:&view=system:nope", "?view=system"),
    ],
)
def test_an_unknown_part_is_dropped_from_the_address_bar(page: Page, base_url: str, console_url: str, junk: str, applied: str) -> None:
    wid = _a_workspace_id(base_url)
    open_shell(page, console_url, wid)

    _go(page, f"#/w/{wid}{junk}")

    expected = f"#/w/{wid}{applied}"
    _settles(page, f"h === {expected!r}".replace("'", '"'), expected)


@pytest.mark.ui_e2e
def test_a_path_outside_the_grammar_does_not_stay_in_the_address_bar(page: Page, base_url: str, console_url: str) -> None:
    wid = _a_workspace_id(base_url)
    open_shell(page, console_url, wid)

    _go(page, "#/zzz")

    _settles(page, 'h.indexOf("zzz") < 0 && h.indexOf("#/") === 0', "a hash without the junk path")


@pytest.mark.ui_e2e
def test_a_valid_link_is_left_exactly_as_it_was(page: Page, base_url: str, console_url: str) -> None:
    wid = _a_workspace_id(base_url)
    open_shell(page, console_url, wid)
    link = f"#/w/{wid}?overlay=agents"

    _go(page, link)

    expect(page.get_by_test_id("nv-overlay:agents")).to_be_visible(timeout=20_000)
    assert _hash(page) == link
