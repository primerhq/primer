"""A deep link from before the Platform revamp lands on a page, never on nothing or on an error (Platform revamp Plan 01, Task 7; seams I-2 and I-7; master Review Focus 2).

* ``view=platform:workspaces`` opens the studio: the Workspaces page left the Platform view, and the studio rail's workspace menu has New session, Settings and Delete. The address bar is
  rewritten without ``view=``.
* ``view=platform:templates`` opens the Workspace templates page. That page is a custom page (seam I-2) that exists while ``window.WT_TemplatesPage`` is loaded (Plan 08 ships it); until then a stand-in
  (``install_templates_page_stand_in``) installed before the bundle runs plays it, and the real bundle overrides the stand-in once it ships. The journey asserts the frame ``nv-plat-page:templates``
  and its current sidebar row, never what the stand-in draws.
* ``view=system:setup`` opens the System dashboard (the grammar has no setup nav; Plan 10 Task 8 takes the Setup page out of the System view), and the address reads ``view=system:dashboard``.
* ``overlay=providers:tts``, the old provider-class link, still opens the Providers overlay at that class, and ``overlay=workspaces::<id>`` still opens the workspace's settings.
* ``view=platform:web`` before Plan 04 ships opens Providers (every type) with no blank page and no error (Review Focus 5).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import install_templates_page_stand_in, open_legacy_route, open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open(page: Page, console_url: str, view: str, marker: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation), so wait for it and
    navigate once more if the page is not there after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    target = page.get_by_test_id(marker)
    for attempt in (1, 2):
        open_view(page, console_url, "primer", view)
        try:
            expect(target).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def _first_workspace(base_url: str) -> str:
    import httpx

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


def test_an_old_workspaces_link_opens_the_studio(page: Page, console_url: str) -> None:
    _open(page, console_url, "platform:providers", "nv-plat-page:providers")      # a view that is not the studio, so the redirect has something to change

    open_view(page, console_url, "primer", "platform:workspaces")

    expect(page.get_by_test_id("nv-view:studio")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-platform")).to_have_count(0)
    expect(page).not_to_have_url(re.compile(r"view="))


def test_an_old_templates_link_opens_the_templates_page(page: Page, console_url: str) -> None:
    install_templates_page_stand_in(page)
    _open(page, console_url, "platform:templates", "nv-plat-page:templates")

    expect(page.get_by_test_id("nv-plat-page:templates")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-plat-row:templates")).to_have_attribute("aria-current", "page")
    expect(page.get_by_test_id("nv-plat-row:templates")).to_contain_text("Workspace templates")
    expect(page.get_by_test_id("nv-plat-row:workspaces")).to_have_count(0)
    expect(page).to_have_url(re.compile(r"[?&]view=platform:templates(&.*)?$"))

    page.reload()
    expect(page.get_by_test_id("nv-plat-page:templates")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("nv-plat-row:templates")).to_have_attribute("aria-current", "page")


def test_an_old_setup_link_opens_the_system_dashboard(page: Page, console_url: str) -> None:
    _open(page, console_url, "platform:providers", "nv-plat-page:providers")

    open_view(page, console_url, "primer", "system:setup")

    expect(page.get_by_test_id("nv-sys-page:dashboard")).to_be_visible(timeout=20_000)
    expect(page).to_have_url(re.compile(r"[?&]view=system:dashboard(&.*)?$"))


def test_an_old_provider_class_link_still_opens_that_class(page: Page, console_url: str) -> None:
    open_legacy_route(page, console_url, "providers/tts")

    expect(page.get_by_test_id("nv-overlay:providers")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("provider-body-tts")).to_be_visible(timeout=20_000)
    expect(page).to_have_url(re.compile(r"overlay=providers:tts"))


def test_an_old_workspace_settings_link_still_opens_the_workspace(page: Page, console_url: str, base_url: str) -> None:
    wid = _first_workspace(base_url)

    open_legacy_route(page, console_url, f"workspaces/{wid}")

    expect(page.get_by_test_id("nv-overlay:workspaces")).to_be_visible(timeout=20_000)
    expect(page).to_have_url(re.compile(r"overlay=workspaces::" + re.escape(wid)))


@pytest.mark.parametrize("view", ["platform:web", "platform:templates"])
def test_a_custom_page_link_with_no_component_opens_providers(page: Page, console_url: str, view: str) -> None:
    """The bundle of this checkout ships neither page yet; once a plan ships one, its own journey covers the page and this case is dropped for it."""
    raised: list[str] = []
    page.on("pageerror", lambda e: raised.append(str(e)))
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    if page.evaluate("typeof window.WEB_SettingsPage === 'function' || typeof window.WT_TemplatesPage === 'function'"):
        pytest.skip("a custom page has shipped; its own journey covers its link")

    open_view(page, console_url, "primer", view)

    expect(page.get_by_test_id("nv-plat-page:providers")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-plat-row:providers")).to_have_attribute("aria-current", "page")
    assert raised == [], f"a link to a custom page that is not loaded raised: {raised[:2]}"
