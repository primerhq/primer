"""Journey: the profile menu offers Light, Dark and System, and Your profile; the phone's More tab offers them too (platform revamp D3, 2026-10-18).

Before: an unlabelled two-way pill that shrink-wrapped to its text. Once Light or Dark was chosen the console never followed the operating system again, a
phone never restored the theme a user had chosen, and the menu had no way to your own profile. Now a labelled Theme row has three choices: System follows the OS
live and after a reload, Light and Dark do not, the same choices are palette verbs, and Your profile opens System > Profile for every role.

The OS colour scheme is the browser's own (``page.emulate_media``) and nothing is mocked: the choice goes through the real tweaks store and the real inline
bootstrap of ``index.html``. ``tests/ui/test_profile_menu_theme.py`` runs the same helpers in V8; this proves the page. ``data-theme`` is checked on ``<html>``
because it is what the style sheet keys on: it must always be ``light`` or ``dark``, never the word ``system`` (which no token block defines).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_mobile_shell, run_verb

PHONE = {"width": 390, "height": 844}


def _html(page: Page):
    return page.locator("html")


def _shell_ready(page: Page) -> None:
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)


def _open_menu(page: Page) -> None:
    _shell_ready(page)
    page.get_by_test_id("nv-profile-btn").click()
    expect(page.get_by_test_id("nv-profile-menu")).to_be_visible()


def _media_queries_reported(page: Page) -> None:
    """Two animation frames: the browser reports media query changes while it updates the rendering, so once both have run a listener that was going to hear
    the change has heard it. A negative assertion ("it did not repaint") is only worth anything after this."""
    page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


@pytest.mark.ui_e2e
def test_system_follows_the_os_live_and_after_a_reload(page: Page) -> None:
    page.emulate_media(color_scheme="dark")
    _open_menu(page)
    expect(page.get_by_test_id("nv-theme-row")).to_contain_text("Theme")
    page.get_by_test_id("nv-theme:system").click()
    expect(_html(page)).to_have_attribute("data-theme", "dark")

    page.emulate_media(color_scheme="light")
    expect(_html(page)).to_have_attribute("data-theme", "light")   # no reload: System is the OS's word, said again when it changes

    page.reload()
    _shell_ready(page)
    expect(_html(page)).to_have_attribute("data-theme", "light")   # a reload resolves from the OS, it does not keep the first resolved colour
    page.emulate_media(color_scheme="dark")
    expect(_html(page)).to_have_attribute("data-theme", "dark")    # and the reloaded page still follows the OS live

    page.get_by_test_id("nv-profile-btn").click()
    expect(page.get_by_test_id("nv-theme:system")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("nv-theme:light")).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_test_id("nv-theme:dark")).to_have_attribute("aria-pressed", "false")


@pytest.mark.ui_e2e
def test_dark_stays_dark_when_the_os_turns_light(page: Page) -> None:
    page.emulate_media(color_scheme="dark")
    _open_menu(page)
    page.get_by_test_id("nv-theme:dark").click()
    expect(_html(page)).to_have_attribute("data-theme", "dark")
    expect(page.get_by_test_id("nv-theme:dark")).to_have_attribute("aria-pressed", "true")

    page.emulate_media(color_scheme="light")
    page.wait_for_function("() => window.matchMedia('(prefers-color-scheme: light)').matches")
    _media_queries_reported(page)
    expect(_html(page)).to_have_attribute("data-theme", "dark")

    page.reload()
    _shell_ready(page)
    expect(_html(page)).to_have_attribute("data-theme", "dark")


@pytest.mark.ui_e2e
def test_the_theme_control_spans_the_menu(page: Page) -> None:
    """The later ``.nv-seg`` rule is ``display: inline-flex``, which shrink-wrapped the old pill; the Theme row is as wide as the menu's rows."""
    _open_menu(page)
    menu = page.get_by_test_id("nv-profile-menu").bounding_box()
    seg = page.get_by_test_id("nv-theme-seg").bounding_box()
    assert menu and seg
    assert seg["width"] >= menu["width"] - 30, f"the control is {seg['width']}px in a {menu['width']}px menu"


@pytest.mark.ui_e2e
def test_your_profile_opens_system_profile(page: Page) -> None:
    _open_menu(page)
    page.get_by_test_id("nv-go-profile").click()
    expect(page.get_by_test_id("nv-sys-profile")).to_be_visible(timeout=10_000)
    expect(page).to_have_url(re.compile(r"view=system:profile"))
    expect(page.get_by_test_id("nv-profile-menu")).to_have_count(0)


@pytest.mark.ui_e2e
def test_the_theme_verbs_run_from_the_palette(page: Page) -> None:
    page.emulate_media(color_scheme="dark")
    _shell_ready(page)
    expect(_html(page)).to_have_attribute("data-theme", "dark")
    run_verb(page, "Switch To Light Theme")
    expect(_html(page)).to_have_attribute("data-theme", "light")
    run_verb(page, "Switch To System Theme")
    expect(_html(page)).to_have_attribute("data-theme", "dark")   # System under a dark OS


@pytest.mark.ui_e2e
def test_the_phone_offers_the_three_choices_and_your_profile(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    page.emulate_media(color_scheme="light")
    open_mobile_shell(page, console_url)
    page.get_by_role("tab", name="More").click()
    seg = page.get_by_test_id("nv-mob-theme-seg")
    expect(seg).to_be_visible(timeout=10_000)
    expect(seg.get_by_role("button")).to_have_count(3)
    # the buttons are flex containers on the phone, which drop a space character at the start of a text run: the gap after the glyph is its own margin
    glyph_gap = page.evaluate(
        "() => getComputedStyle(document.querySelector('[data-testid=\"nv-mob-theme:light\"] .nv-theme-glyph')).marginRight"
    )
    assert glyph_gap == "5px"

    page.get_by_test_id("nv-mob-theme:dark").click()
    expect(_html(page)).to_have_attribute("data-theme", "dark")
    page.get_by_test_id("nv-mob-theme:system").click()
    expect(_html(page)).to_have_attribute("data-theme", "light")   # the light OS, not the dark the user had just picked

    page.get_by_test_id("nv-mob-setting:profile").click()
    expect(page.get_by_test_id("nv-mob-system-screen")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-sys-profile")).to_be_visible()


@pytest.mark.ui_e2e
def test_the_phone_restores_the_theme_the_user_chose(page: Page, console_url: str) -> None:
    """Another account on this browser chose Dark last (the browser-wide tweak says dark), but this user's own choice is Light. The desktop restored the
    user's own key once the username was known; the phone never did, so it kept the other account's colour."""
    page.set_viewport_size(PHONE)
    open_mobile_shell(page, console_url)
    # the console's own fallback while the status read has no name is "anon"
    username = page.evaluate("() => fetch('/v1/auth/status').then((r) => r.json()).then((s) => s.username)") or "anon"
    page.evaluate(
        "(u) => { localStorage.setItem('primer.theme.' + u, 'light'); localStorage.setItem('primer.tweaks', JSON.stringify({ theme: 'dark' })); }",
        username,
    )
    page.reload()
    expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=20_000)
    expect(_html(page)).to_have_attribute("data-theme", "light", timeout=10_000)
