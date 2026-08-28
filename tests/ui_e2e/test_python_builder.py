"""UI E2E: the python toolset builder.

The static tests assert the pieces exist. These assert they work together in
a browser against a live server, which is the only place the interesting
claims can be checked:

* CodeMirror actually mounts (a 447KB vendored bundle that silently fails to
  load would leave the fallback textarea, and every static test would still
  pass)
* completions offer primer's injected surface
* the dry-run validate route drives lint marks and the function outline
  BEFORE anything is saved
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402

from tests.ui_e2e._python_helpers import set_python_source  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route


GREET = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person by name.\n\n'
    "    Use when you need a friendly greeting.\n\n"
    "    Args:\n        name: Who to greet.\n"
    '    """\n'
    "    return 'hello ' + name\n"
)


def _seed(base_url: str, tid: str, source: str = GREET) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.post(
            "/v1/toolsets",
            json={"id": tid, "provider": "python",
                  "config": {"source": source, "source_version": 1}},
        ).raise_for_status()


def _drop(base_url: str, tid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.delete(f"/v1/toolsets/{tid}")


def _open(page: Page, console_url: str, tid: str) -> None:
    open_legacy_route(page, console_url, f"toolsets/{tid}")
    expect(page.locator('[data-testid="python-editor"]')).to_be_visible(timeout=20_000)


@pytest.mark.ui_e2e
def test_codemirror_mounts_rather_than_the_fallback(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    """If the vendored bundle fails to load, the editor silently degrades to a
    textarea and every static test still passes. This is the only check that
    distinguishes them."""
    tid = f"pyb-mount-{unique_suffix}"
    _seed(base_url, tid)
    try:
        _open(page, console_url, tid)
        surface = page.locator('[data-testid="python-source"]')
        expect(surface).to_be_visible(timeout=15_000)
        assert surface.get_attribute("data-editor") == "codemirror", (
            "window.CM6 did not load; the editor fell back to a textarea"
        )
        # The gutter is CodeMirror's, so its presence means the real editor
        # mounted rather than an empty themed div.
        expect(page.locator(".cm-gutters")).to_be_visible(timeout=10_000)
        expect(page.locator(".cm-content")).to_contain_text("greet")
    finally:
        _drop(base_url, tid)


@pytest.mark.ui_e2e
def test_the_outline_lists_functions_from_the_unsaved_draft(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    """The outline is fed by the dry-run validate route, so a function that
    has only been typed -- never saved -- still appears."""
    tid = f"pyb-outline-{unique_suffix}"
    _seed(base_url, tid)
    try:
        _open(page, console_url, tid)
        # R4 review nit: the row testid is now suffixed with the tool
        # id/fn_name (was a static, shared-across-all-rows testid) - a
        # prefix selector still matches every row.
        rows = page.locator('[data-testid^="python-outline-row:"]')
        expect(rows.first).to_be_visible(timeout=15_000)
        expect(rows.first).to_contain_text("greet")

        # Set the document rather than typing it: auto-indent rewrites source
        # as it arrives, so a typed docstring is not the docstring you wrote.
        set_python_source(page, GREET + (
            "\n\n@primer_tool()\n"
            "def farewell(who: str) -> str:\n"
            '    """Say goodbye.\n\n'
            "    Use when parting.\n\n"
            "    Args:\n        who: Who to bid farewell.\n"
            '    """\n'
            "    return who\n"
        ))
        # Debounced validate (450ms) then a re-render.
        expect(rows).to_have_count(2, timeout=15_000)
        expect(page.locator('[data-testid="python-outline"]')).to_contain_text(
            "farewell"
        )

        # Nothing was saved, so the callable set is still just the seeded one.
        saved = page.locator('[data-testid^="python-tool-row:"]')
        expect(saved).to_have_count(1)
    finally:
        _drop(base_url, tid)


@pytest.mark.ui_e2e
def test_a_broken_docstring_marks_the_line_without_saving(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    """The write-blind-then-save loop this feature exists to remove."""
    tid = f"pyb-lint-{unique_suffix}"
    _seed(base_url, tid)
    try:
        _open(page, console_url, tid)
        expect(page.locator('[data-testid="python-live-status"]')).to_have_attribute(
            "data-ok", "1", timeout=15_000,
        )

        # An undocumented parameter: registration names it.
        set_python_source(page, GREET + (
            "\n\n@primer_tool()\n"
            "def broken(a: str, b: str) -> str:\n"
            '    """Do a thing.\n\n'
            "    Use when you must.\n\n"
            "    Args:\n        a: Only a is documented.\n"
            '    """\n'
            "    return a\n"
        ))
        expect(page.locator('[data-testid="python-live-status"]')).to_have_attribute(
            "data-ok", "0", timeout=15_000,
        )
        # The failure is on a line, not just in a panel.
        expect(page.locator(".cm-lintRange-error").first).to_be_visible(timeout=10_000)
        expect(page.locator('[data-testid="python-outline-error"]')).to_be_visible()
    finally:
        _drop(base_url, tid)


@pytest.mark.ui_e2e
def test_add_function_inserts_a_scaffold_that_registers(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    """A scaffold that does not itself register would teach the wrong shape."""
    tid = f"pyb-scaffold-{unique_suffix}"
    _seed(base_url, tid, source="")
    try:
        _open(page, console_url, tid)
        expect(page.locator('[data-testid="python-outline-empty"]')).to_be_visible(
            timeout=15_000,
        )

        page.locator('[data-testid="python-add-function"]').click()
        page.locator('[data-testid="python-scaffold-tool"]').click()

        expect(page.locator(".cm-content")).to_contain_text("my_tool", timeout=10_000)
        # The comments are the feature, not filler.
        expect(page.locator(".cm-content")).to_contain_text("timeout_seconds")
        # And it registers as-is.
        expect(page.locator('[data-testid="python-live-status"]')).to_have_attribute(
            "data-ok", "1", timeout=15_000,
        )
        expect(page.locator('[data-testid^="python-outline-row:"]').first
               ).to_contain_text("my_tool")
    finally:
        _drop(base_url, tid)


@pytest.mark.ui_e2e
def test_the_yielding_scaffold_is_marked_as_yielding(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    tid = f"pyb-yield-{unique_suffix}"
    _seed(base_url, tid, source="")
    try:
        _open(page, console_url, tid)
        page.locator('[data-testid="python-add-function"]').click()
        page.locator('[data-testid="python-scaffold-yielding"]').click()

        expect(page.locator('[data-testid="python-live-status"]')).to_have_attribute(
            "data-ok", "1", timeout=15_000,
        )
        # Parking a run is the most important thing to know at a glance.
        # The single ad-hoc "yields" pill was replaced by the shared
        # y/w/r/n CapabilityBadges component (R4 Workbench group) so this
        # tool's badge row carries the same y/w/r/n language as every
        # other tool picker in the console; the active "y" badge is the
        # equivalent signal.
        yields_badge = page.locator(
            '[data-testid^="python-outline-row:"] [data-testid="cap-badge-y"]'
        ).first
        expect(yields_badge).to_be_visible(timeout=10_000)
        expect(yields_badge).to_have_attribute("data-on", "true")
    finally:
        _drop(base_url, tid)


@pytest.mark.ui_e2e
def test_completions_offer_primers_injected_surface(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    """The names an operator cannot look up in any Python documentation."""
    tid = f"pyb-complete-{unique_suffix}"
    _seed(base_url, tid)
    try:
        _open(page, console_url, tid)
        expect(page.locator(".cm-content")).to_be_visible(timeout=15_000)

        page.locator(".cm-content").click()
        page.keyboard.press("Control+End")
        page.keyboard.type("\nask_u")
        options = page.locator(".cm-tooltip-autocomplete li")
        expect(options.first).to_be_visible(timeout=10_000)
        expect(options.first).to_contain_text("ask_user")
    finally:
        _drop(base_url, tid)
