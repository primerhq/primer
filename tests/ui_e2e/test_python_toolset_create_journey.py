"""Creating a Python toolset says what happened and opens the editor, not "Connected" (finding ADM-11 of the 2026-10-08 admin review).

The create dialog showed "Registered <id> ... checking the connection", a spinner ("Connecting to the MCP server and importing tools") and
"Connected. 0 tools imported." for a Python toolset, which has no connection: its tools are the functions in the source the operator writes AFTER the create.
Driven on the real Platform > Toolsets page against the real server: the result says the toolset was created, that there is nothing to connect and that
the editor comes next; there is no connection claim and no probe request; Done is "Open the editor" and lands on the toolset's detail, where the Python
editor is.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_toolsets(page, console_url: str) -> None:
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-plat-page:toolsets")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", "platform:toolsets")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def test_a_python_create_says_there_is_nothing_to_connect_and_opens_the_editor(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    toolset_id = f"ts-adm11-{unique_suffix}"
    probes: list[str] = []
    page.on("request", lambda r: probes.append(r.url) if r.method == "GET" and r.url.endswith(f"/v1/toolsets/{toolset_id}/tools") else None)
    try:
        _open_toolsets(page, console_url)
        page.get_by_test_id("nv-plat-create").click()
        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text("New toolset", timeout=10_000)
        dialog.get_by_placeholder("auto-generated").fill(toolset_id)
        dialog.locator("select.select").select_option("python")
        dialog.get_by_role("button", name="Create", exact=True).click()

        result = page.get_by_test_id("toolset-connect-result")
        expect(result).to_be_visible(timeout=20_000)
        shown = result.inner_text()
        assert toolset_id in shown and "Created" in shown, shown
        assert "no connection to check" in shown and "write its source" in shown, shown
        for claim in ("Connected", "Connecting", "checking the connection", "tools imported", "MCP server"):
            assert claim not in shown, f"{claim!r} in {shown!r}"
        assert probes == [], f"a connection probe ran for a Python toolset: {probes}"

        done = page.get_by_test_id("toolset-connect-done")
        expect(done).to_have_text("Open the editor")
        expect(done).to_be_enabled()
        done.click()

        # The detail opens on the Config tab, which for a Python toolset IS the editor.
        expect(page.get_by_test_id("nv-overlay-body")).to_contain_text(toolset_id, timeout=15_000)
        expect(page.get_by_test_id("python-isolation-level")).to_be_visible(timeout=15_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/toolsets/{toolset_id}")
