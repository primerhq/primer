"""Platform > Providers: a provider type is part of the address and of the sidebar (Platform revamp Plan 01, Task 6; seam I-1; Review Focus 5).

The Providers row has a sub-entry per provider type. ``view=platform:providers:<type>`` opens the catalogue at that type, survives a reload, and is what a click on the sub-entry
writes; an address naming a type the grammar does not list is rewritten to ``view=platform:providers`` and shows every type (the catalogue's "All" body), with no blank page and no error.

* ``test_a_type_link_opens_that_type_and_survives_a_reload`` opens ``view=platform:providers:tts``: ``provider-body-tts`` is shown, ``nv-plat-sub:tts`` is the current entry and the Providers row is
  not; the same after a reload.
* ``test_clicking_a_type_writes_it_into_the_address`` clicks ``nv-plat-sub:llm`` and ``nv-plat-row:providers``: the address reads ``view=platform:providers:llm``, then ``view=platform:providers``.
* ``test_an_unknown_type_is_rewritten_to_all`` opens ``view=platform:providers:bogus``.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _address(view: str) -> re.Pattern[str]:
    """The address bar carries exactly this view (and nothing else after it in the same parameter)."""
    return re.compile(r".*[?&]view=" + re.escape(view) + r"(&.*)?$")


def _open(page: Page, console_url: str, view: str, body: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation), so wait for it and
    navigate once more if the page is not there after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id(body)
    for attempt in (1, 2):
        open_view(page, console_url, "primer", view)
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def test_a_type_link_opens_that_type_and_survives_a_reload(page: Page, console_url: str) -> None:
    _open(page, console_url, "platform:providers:tts", "provider-body-tts")
    expect(page.get_by_test_id("nv-plat-sub:tts")).to_have_attribute("aria-current", "page")
    expect(page.get_by_test_id("nv-plat-row:providers")).not_to_have_attribute("aria-current", "page")
    expect(page).to_have_url(_address("platform:providers:tts"))

    page.reload()
    expect(page.get_by_test_id("provider-body-tts")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("nv-plat-sub:tts")).to_have_attribute("aria-current", "page")
    expect(page).to_have_url(_address("platform:providers:tts"))


def test_clicking_a_type_writes_it_into_the_address(page: Page, console_url: str) -> None:
    _open(page, console_url, "platform:providers", "provider-body-all")
    expect(page.get_by_test_id("nv-plat-row:providers")).to_have_attribute("aria-current", "page")

    page.get_by_test_id("nv-plat-sub:llm").click()
    expect(page.get_by_test_id("provider-body-llm")).to_be_visible(timeout=15_000)
    expect(page).to_have_url(_address("platform:providers:llm"))
    expect(page.get_by_test_id("nv-plat-sub:llm")).to_have_attribute("aria-current", "page")
    expect(page.get_by_test_id("nv-plat-row:providers")).not_to_have_attribute("aria-current", "page")

    page.get_by_test_id("nv-plat-row:providers").click()
    expect(page.get_by_test_id("provider-body-all")).to_be_visible(timeout=15_000)
    expect(page).to_have_url(_address("platform:providers"))
    expect(page.get_by_test_id("nv-plat-row:providers")).to_have_attribute("aria-current", "page")


def test_an_unknown_type_is_rewritten_to_all(page: Page, console_url: str) -> None:
    raised: list[str] = []
    page.on("pageerror", lambda e: raised.append(str(e)))

    _open(page, console_url, "platform:providers:bogus", "provider-body-all")
    expect(page).to_have_url(_address("platform:providers"))
    expect(page.get_by_test_id("nv-plat-row:providers")).to_have_attribute("aria-current", "page")
    assert raised == [], f"an unknown type raised: {raised[:2]}"
