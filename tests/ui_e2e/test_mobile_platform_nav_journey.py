"""On a phone, the Platform sections are the sidebar's entries (Platform revamp Plan 01, Task 8; seam I-2 on phones; design 4.3).

390x844. The More tab used to list only the navs that had a generic page-table entry, so a phone had no Providers section and could never reach a custom page.
Now it lists every nav the desktop sidebar draws:

* ``test_the_sections_are_the_sidebars_entries`` - ``nv-mob-plat-nav:profiles`` reads "LLM Profiles", there is no Workspaces section (it left the Platform view)
  and Providers is a section.
* ``test_providers_lists_its_types_and_a_type_shows_its_catalog`` - Providers opens a second-level list (All providers, then the ten types in the sidebar's
  order); a type shows the catalogue at that type (``provider-body-tts``); Back returns to the types, then to the sections.
* ``test_a_provider_type_link_opens_that_type`` - ``view=platform:providers:tts`` lands on the More tab with the TTS catalogue open, and the link is consumed.
* ``test_a_custom_page_is_a_section_only_while_its_component_is_loaded`` - with a stand-in ``window.WEB_SettingsPage`` installed before the bundle runs, Web is
  listed and opens under Back; without a component there is no Web row. The real page replaces the stand-in the day Plan 04 ships it, so the journey asserts
  the phone's frame (``nv-mob-plat-page:web``) and never what the component draws.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import boot_mobile_shell, open_mobile_shell

pytestmark = smk("SMK-UI-01", status="partial")

PHONE = {"width": 390, "height": 844}
TYPES = ["llm", "embedding", "ssp", "cross_encoder", "workspace", "web_search", "channel", "artifact_storage", "tts", "stt"]
LABELS = ["LLM", "Embedding", "Semantic Search Storage", "Cross Encoding", "Workspace", "Web Search", "Channels", "Artifact Store", "TTS", "ASR"]

# What Plan 04's Web page will be: a function on ``window`` that draws something. React is global by the time it is called (the bundle has loaded), not when this script runs.
WEB_PAGE_STAND_IN = """
window.WEB_SettingsPage = function () {
  return window.React.createElement("div", { "data-testid": "web-stand-in" }, "Web");
};
"""


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


def _open_more(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    open_mobile_shell(page, console_url)
    page.get_by_role("tab", name="More").click()
    expect(page.get_by_test_id("nv-mob-plat-sections")).to_be_visible(timeout=10_000)


def _ids(page: Page, prefix: str) -> list[str]:
    return page.locator(f"[data-testid^='{prefix}']").evaluate_all(
        "(els, prefix) => els.map((e) => e.getAttribute('data-testid').slice(prefix.length))", prefix,
    )


def test_the_sections_are_the_sidebars_entries(page: Page, console_url: str) -> None:
    _open_more(page, console_url)

    expect(page.get_by_test_id("nv-mob-plat-nav:profiles")).to_have_text("LLM Profiles")
    expect(page.get_by_test_id("nv-mob-plat-nav:providers")).to_have_text("Providers")
    expect(page.get_by_test_id("nv-mob-plat-nav:channels")).to_have_text("Channels")
    expect(page.get_by_test_id("nv-mob-plat-nav:workspaces")).to_have_count(0)


def test_providers_lists_its_types_and_a_type_shows_its_catalog(page: Page, console_url: str) -> None:
    _open_more(page, console_url)

    page.get_by_test_id("nv-mob-plat-nav:providers").click()
    expect(page.get_by_test_id("nv-mob-plat-page:providers")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-mob-plat-sub:all")).to_have_text("All providers")
    assert _ids(page, "nv-mob-plat-sub:") == ["all"] + TYPES, "All providers first, then the ten types in the sidebar's order"
    for t, label in zip(TYPES, LABELS):
        expect(page.get_by_test_id(f"nv-mob-plat-sub:{t}")).to_have_text(label)
    expect(page.get_by_test_id("nv-mob-profile")).to_have_count(0)   # the section fills the tab

    page.get_by_test_id("nv-mob-plat-sub:tts").click()
    expect(page.get_by_test_id("provider-body-tts")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-mob-plat-title")).to_have_text("TTS")

    page.get_by_test_id("nv-mob-plat-back").click()
    expect(page.get_by_test_id("nv-mob-plat-sub:all")).to_be_visible()
    expect(page.get_by_test_id("provider-body-tts")).to_have_count(0)

    page.get_by_test_id("nv-mob-plat-back").click()
    expect(page.get_by_test_id("nv-mob-plat-sections")).to_be_visible()
    expect(page.get_by_test_id("nv-mob-profile")).to_be_visible()


def test_a_provider_type_link_opens_that_type(page: Page, base_url: str, console_url: str) -> None:
    wid = _a_workspace_id(base_url)
    page.set_viewport_size(PHONE)
    boot_mobile_shell(page, f"{console_url}#/w/{wid}?view=platform:providers:tts")

    expect(page.get_by_test_id("nv-mob-plat-page:providers")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("provider-body-tts")).to_be_visible(timeout=20_000)
    expect(page).not_to_have_url(re.compile(r"view=platform"), timeout=10_000)   # consumed: the URL no longer names it

    page.get_by_test_id("nv-mob-plat-back").click()
    expect(page.get_by_test_id("nv-mob-plat-sub:tts")).to_be_visible()   # Back from a type is the list of types, and it stays there


def test_a_custom_page_is_a_section_only_while_its_component_is_loaded(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    open_mobile_shell(page, console_url)
    web_loaded = page.evaluate("typeof window.WEB_SettingsPage === 'function'")
    page.get_by_role("tab", name="More").click()
    expect(page.get_by_test_id("nv-mob-plat-sections")).to_be_visible(timeout=10_000)
    if not web_loaded:
        expect(page.get_by_test_id("nv-mob-plat-nav:web")).to_have_count(0)   # no placeholder row

    page.add_init_script(WEB_PAGE_STAND_IN)
    page.reload()
    expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=20_000)
    page.get_by_role("tab", name="More").click()
    expect(page.get_by_test_id("nv-mob-plat-nav:web")).to_have_text("Web", timeout=10_000)

    page.get_by_test_id("nv-mob-plat-nav:web").click()
    expect(page.get_by_test_id("nv-mob-plat-page:web")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-mob-plat-title")).to_have_text("Web")

    page.get_by_test_id("nv-mob-plat-back").click()
    expect(page.get_by_test_id("nv-mob-plat-sections")).to_be_visible()
