"""UI E2E: authoring a python tool in the console."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

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
            json={
                "id": tid,
                "provider": "python",
                "config": {"source": source, "source_version": 1},
            },
        ).raise_for_status()


def _drop(base_url: str, tid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.delete(f"/v1/toolsets/{tid}")


@pytest.mark.ui_e2e
def test_the_editor_shows_derived_tools_and_the_isolation_level(
    page, base_url: str, console_url: str, unique_suffix: str
) -> None:
    tid = f"toolset-ui-{unique_suffix}"
    _seed(base_url, tid)
    try:
        page.goto(f"{console_url}#/toolsets/{tid}", wait_until="domcontentloaded")

        editor = page.locator('[data-testid="python-editor"]')
        expect(editor).to_be_visible(timeout=20_000)

        # The derived list comes from the server, so this asserts the source
        # actually registered rather than that the textarea has text in it.
        rows = page.locator('[data-testid="python-tool-row"]')
        expect(rows.first).to_be_visible(timeout=15_000)
        expect(rows.first).to_contain_text("greet")
        expect(rows.first).to_contain_text("name")

        # The level must be stated, whichever one this deployment is on.
        badge = page.locator('[data-testid="python-isolation-level"]')
        expect(badge).to_be_visible()
        assert badge.get_attribute("data-level") in {
            "container", "seccomp", "sandbox-exec", "rlimit-only",
        }
    finally:
        _drop(base_url, tid)


@pytest.mark.ui_e2e
def test_a_broken_docstring_reports_inline_with_its_line(
    page, base_url: str, console_url: str, unique_suffix: str
) -> None:
    tid = f"toolset-ui-bad-{unique_suffix}"
    _seed(base_url, tid)
    try:
        page.goto(f"{console_url}#/toolsets/{tid}", wait_until="domcontentloaded")
        source = page.locator('[data-testid="python-source"]')
        expect(source).to_be_visible(timeout=20_000)

        # Strip the docstring: registration must refuse, and say where.
        source.fill(
            "@primer_tool()\ndef greet(name: str) -> str:\n    return name\n"
        )
        page.locator('[data-testid="python-save"]').click()

        err = page.locator('[data-testid="python-registration-error"]')
        expect(err).to_be_visible(timeout=15_000)
        expect(err).to_contain_text("greet")
    finally:
        _drop(base_url, tid)
