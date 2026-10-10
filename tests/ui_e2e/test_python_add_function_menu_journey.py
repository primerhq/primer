"""Journey: the Python editor's "Add function" menu answers Escape, and the editor is named and explains how to leave it (board task 01a125ec-4e03, found in the review of #728).

With the menu open one Escape closed the whole toolsets overlay and the code that was changed and not saved went with it. The menu is a layer of the console's Escape stack now: the Escape closes it, the overlay and the
draft stay, and focus is on the button that opened it. The second test checks the editor in a browser: the content has a name, it is described by a hint, and every way out the hint names works (Escape then Tab; a
SECOND Escape after one that only collapsed a selection; Ctrl+M).
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402

from tests.ui_e2e._python_helpers import set_python_source  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route  # noqa: E402

SOURCE = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person by name.\n\n'
    "    Use when you need a friendly greeting.\n\n"
    "    Args:\n        name: Who to greet.\n"
    '    """\n'
    "    return 'hello ' + name\n"
)
_IN_EDITOR = "() => !!document.activeElement.closest('.cm-editor')"
_ACTIVE_TESTID = "() => document.activeElement && document.activeElement.getAttribute ? document.activeElement.getAttribute('data-testid') : null"


def _create(base_url: str, tid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.post("/v1/toolsets", json={"id": tid, "provider": "python", "config": {"source": SOURCE, "source_version": 1}}).raise_for_status()


def _delete(base_url: str, tid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.delete(f"/v1/toolsets/{tid}")


def _open_editor(page: Page, console_url: str, tid: str) -> None:
    open_legacy_route(page, console_url, f"toolsets/{tid}")
    expect(page.locator('[data-testid="python-editor"]')).to_be_visible(timeout=20_000)
    expect(page.locator(".cm-content")).to_be_visible(timeout=15_000)


@pytest.mark.ui_e2e
def test_escape_with_the_add_function_menu_open_closes_the_menu_and_not_the_overlay(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    tid = f"pyadd-{unique_suffix}"
    _create(base_url, tid)
    try:
        _open_editor(page, console_url, tid)
        content = page.locator(".cm-content")
        set_python_source(page, SOURCE + "# not saved yet\n")
        expect(content).to_contain_text("# not saved yet")
        overlay = page.get_by_role("dialog")
        expect(overlay).to_be_visible()

        page.get_by_test_id("python-add-function").click()
        menu = page.get_by_test_id("python-add-function-menu")
        expect(menu).to_be_visible(timeout=5_000)

        page.keyboard.press("Escape")
        expect(menu).to_have_count(0, timeout=5_000)
        expect(overlay).to_be_visible()                                                  # the overlay stays ...
        expect(content).to_contain_text("# not saved yet")                               # ... with the draft
        assert page.evaluate(_ACTIVE_TESTID) == "python-add-function", "focus did not go back to the button that opened the menu"
    finally:
        _delete(base_url, tid)


@pytest.mark.ui_e2e
def test_the_editor_is_named_described_and_every_way_out_it_names_works(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    tid = f"pyname-{unique_suffix}"
    _create(base_url, tid)
    try:
        _open_editor(page, console_url, tid)
        content = page.locator(".cm-content")
        assert "python" in (content.get_attribute("aria-label") or "").lower(), "the editable content has no accessible name"
        hint_id = content.get_attribute("aria-describedby")
        assert hint_id, "the editable content has no description"
        hint = page.locator(f"#{hint_id}")
        assert hint.count() == 1
        text = hint.inner_text().lower()
        assert "escape" in text and "tab" in text and "ctrl" in text, text

        before = content.inner_text()
        # Escape, then Tab
        content.click()
        page.keyboard.press("Escape")
        page.keyboard.press("Tab")
        assert not page.evaluate(_IN_EDITOR), "Escape then Tab did not leave the editor"
        assert content.inner_text() == before

        # a first Escape that only collapses a selection is not the way out; the SECOND is
        content.click()
        page.keyboard.press("Control+a")
        page.keyboard.press("Escape")                                                    # collapses the selection
        page.keyboard.press("Escape")                                                    # starts the tab-focus mode
        page.keyboard.press("Tab")
        assert not page.evaluate(_IN_EDITOR), "a second Escape then Tab did not leave the editor"
        assert content.inner_text() == before

        # Ctrl+M turns the tab-focus mode on
        content.click()
        page.keyboard.press("Control+m")
        page.keyboard.press("Tab")
        assert not page.evaluate(_IN_EDITOR), "Ctrl+M then Tab did not leave the editor"
        assert content.inner_text() == before
    finally:
        _delete(base_url, tid)
