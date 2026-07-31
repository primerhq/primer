"""Helpers for driving the python toolset editor from Playwright.

The editing surface is a CodeMirror instance, not a textarea, so
``locator.fill()`` does not work on it and ``keyboard.type()`` is actively
wrong for source: auto-indent and bracket-closing rewrite what you send, so
the document you get is not the document you asked for.

:func:`set_python_source` sets the document exactly by dispatching a
transaction through the view, which is also how a paste behaves.
"""

from __future__ import annotations

from playwright.sync_api import Page

SOURCE = '[data-testid="python-source"]'


def is_codemirror(page: Page) -> bool:
    el = page.locator(SOURCE)
    return el.get_attribute("data-editor") == "codemirror"


def set_python_source(page: Page, text: str) -> None:
    """Replace the editor's whole document with ``text``.

    Works against either surface, so a test does not have to care whether the
    vendored bundle loaded.
    """
    page.locator(SOURCE).wait_for(state="visible", timeout=20_000)

    if not is_codemirror(page):
        page.locator(SOURCE).fill(text)
        return

    page.wait_for_selector(".cm-content", timeout=15_000)
    ok = page.evaluate(
        """
        (text) => {
          const el = document.querySelector('.cm-editor');
          if (!el || !window.CM6 || !window.CM6.EditorView) return false;
          const view = window.CM6.EditorView.findFromDOM(el);
          if (!view) return false;
          view.dispatch({
            changes: {from: 0, to: view.state.doc.length, insert: text},
          });
          return view.state.doc.toString() === text;
        }
        """,
        text,
    )
    assert ok, "could not set the CodeMirror document"
