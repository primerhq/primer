"""The login screen explains a failed SSO sign-in the server sent the browser back with (ADM-24b).

``GET /v1/auth/sso/<id>/login`` and ``/callback`` send a failed browser sign-in to ``/console/?sso_error=<code>``
(``tests/api/test_sso_browser_failures.py``). This drives the real login screen: the banner reads the code, the address bar
loses it, and a reload does not bring the failure back.

The shared ui_e2e install runs with auth off, so the login screen never shows on its own. Only the two endpoints the auth gate
and the login screen read are answered by a small fake behind ``page.route`` (the technique of ``test_setup_wizard_resume_journey``);
the console's static assets and scripts come from the real server, so the JSX under test is the shipped JSX.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_console_with_query

pytestmark = smk("SMK-UI-06", status="partial")


def _show_the_login_screen(page) -> None:
    def status(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "has_user": True, "authenticated": False, "setup_complete": True, "setup_missing": [],
        }))

    def providers(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps([{"id": "corp", "name": "Corp IdP"}]))

    page.route("**/v1/auth/status", status)
    page.route("**/v1/auth/sso/providers", providers)


def test_a_failed_sso_sign_in_shows_a_banner_and_clears_the_address_bar(page, console_url: str) -> None:
    _show_the_login_screen(page)

    open_console_with_query(page, console_url, "?sso_error=sso_jit_disabled")

    banner = page.locator(".auth-banner")
    expect(banner).to_be_visible(timeout=20_000)
    expect(banner).to_contain_text("No account is linked to this identity")
    expect(banner).to_contain_text("just-in-time provisioning")
    expect(page.get_by_role("button", name="Sign in with Corp IdP")).to_be_visible()
    assert "sso_error" not in page.url, page.url

    page.reload()
    expect(page.get_by_role("button", name="Sign in with Corp IdP")).to_be_visible(timeout=20_000)
    expect(page.locator(".auth-banner")).to_have_count(0)


def test_a_code_the_screen_does_not_know_gets_the_generic_sentence_and_is_not_echoed(page, console_url: str) -> None:
    _show_the_login_screen(page)

    open_console_with_query(page, console_url, "?sso_error=%3Cb%3Eboom%3C%2Fb%3E")

    banner = page.locator(".auth-banner")
    expect(banner).to_be_visible(timeout=20_000)
    expect(banner).to_contain_text("Single sign-on did not complete")
    expect(banner).not_to_contain_text("boom")


def test_a_wrong_password_replaces_the_sso_banner(page, console_url: str) -> None:
    _show_the_login_screen(page)
    page.route("**/v1/auth/login", lambda route: route.fulfill(
        status=401, content_type="application/json",
        body=json.dumps({"type": "/errors/authentication-failed", "title": "Authentication Failed", "status": 401,
                         "detail": "invalid_credentials"}),
    ))
    open_console_with_query(page, console_url, "?sso_error=missing_code")
    expect(page.locator(".auth-banner")).to_contain_text("Sign-in was not completed", timeout=20_000)

    page.get_by_label("Username").fill("someone")
    page.locator("input[autocomplete=current-password]").fill("wrong-password")
    page.get_by_role("button", name=re.compile(r"^Sign in$")).click()

    expect(page.locator(".auth-banner")).to_contain_text("Invalid username or password", timeout=10_000)
    expect(page.locator(".auth-banner")).not_to_contain_text("not completed")
