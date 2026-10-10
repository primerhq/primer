"""Journey: Escape in the Python code editor keeps the toolsets overlay open, and the Tab after it leaves the editor (board task 01a121a5-c13c, found in the review of #700).

CodeMirror's way out of an editor that indents on Tab is Escape, then Tab: an Escape keydown puts the editor in tab-focus mode for two seconds, and the next Tab moves focus on instead of indenting. CodeMirror does not call
``preventDefault`` on that Escape, so the console's Escape stack (#700) took it for "close the top layer" and closed the toolsets overlay, discarding the code that was changed and not saved. The editor now consumes a plain
Escape, and the stack ignores what was handled.

The test changes the code of a real python toolset in the real overlay through a transaction, as a paste does (typing opens the completion list, whose own Escape closes the list and would hide the fault), and presses the
keys. Where the Tab goes once it has left the editor is the focus trap's business (an element after the last tab stop of the dialog, #717), not this fix's.
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


@pytest.mark.ui_e2e
def test_escape_in_the_python_editor_keeps_the_overlay_and_the_next_tab_leaves_the_editor(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    tid = f"pyesc-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.post("/v1/toolsets", json={"id": tid, "provider": "python", "config": {"source": SOURCE, "source_version": 1}}).raise_for_status()
    try:
        open_legacy_route(page, console_url, f"toolsets/{tid}")
        expect(page.locator('[data-testid="python-editor"]')).to_be_visible(timeout=20_000)
        content = page.locator(".cm-content")
        expect(content).to_be_visible(timeout=15_000)
        set_python_source(page, SOURCE + "# not saved yet\n")
        expect(content).to_contain_text("# not saved yet")
        content.click()
        page.keyboard.press("Control+End")
        overlay = page.get_by_role("dialog")
        expect(overlay).to_be_visible()
        before = content.inner_text()

        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        expect(overlay).to_be_visible()
        expect(content).to_contain_text("# not saved yet")
        assert page.evaluate(_IN_EDITOR), "Escape moved focus out of the editor by itself"

        page.keyboard.press("Tab")
        assert not page.evaluate(_IN_EDITOR), "the Tab after Escape did not leave the editor"
        assert content.inner_text() == before, "the Tab indented the code instead of leaving the editor"
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/toolsets/{tid}")
