"""The registration card is truthful and a bad username is named before any request (console review C-002 and C-006).

The shared ui_e2e install runs with auth off, so the register screen never shows on its own: the endpoint the auth gate reads is answered
by a small fake behind ``page.route`` (the technique of ``test_login_sso_error_journey``); the console's scripts come from the real server.
``POST /v1/auth/register`` is also routed, to count the requests that would have reached it.
"""

from __future__ import annotations

import json

from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_gate

pytestmark = smk("SMK-UI-06", status="partial")


def _show_the_register_screen(page, console_url: str) -> list[str]:
    posts: list[str] = []
    page.route("**/v1/auth/status", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"has_user": False, "authenticated": False, "setup_complete": False, "setup_missing": []}),
    ))
    page.route("**/v1/auth/sso/providers", lambda route: route.fulfill(status=200, content_type="application/json", body="[]"))

    def register(route) -> None:
        posts.append(route.request.url)
        route.fulfill(status=500, content_type="application/json", body="{}")

    page.route("**/v1/auth/register", register)
    open_gate(page, console_url)
    return posts


def test_the_card_says_the_first_account_is_the_administrator_and_where_users_and_sso_live(page, console_url: str) -> None:
    _show_the_register_screen(page, console_url)

    expect(page.get_by_role("heading", name="Create the operator account")).to_be_visible(timeout=20_000)
    card = page.locator(".auth-card")
    expect(card).to_contain_text("This first account is the administrator")
    expect(card).to_contain_text("under System")
    expect(card).not_to_contain_text("later release")


def test_a_username_with_a_space_is_named_beside_the_field_and_never_sent(page, console_url: str) -> None:
    posts = _show_the_register_screen(page, console_url)
    expect(page.get_by_role("heading", name="Create the operator account")).to_be_visible(timeout=20_000)

    page.get_by_label("Username").fill("Reviewer Name")
    page.locator("input[autocomplete=new-password]").nth(0).fill("long-enough-1")
    page.locator("input[autocomplete=new-password]").nth(1).fill("long-enough-1")
    page.get_by_role("button", name="Create account").click()

    err = page.locator(".field-err")
    expect(err).to_have_count(1)
    expect(err).to_contain_text("username must be 1 to 64 characters")
    expect(page.locator(".auth-banner")).to_have_count(0)
    assert posts == [], f"the server should never have been asked: {posts}"


def test_a_short_password_names_the_password(page, console_url: str) -> None:
    posts = _show_the_register_screen(page, console_url)
    expect(page.get_by_role("heading", name="Create the operator account")).to_be_visible(timeout=20_000)

    page.get_by_label("Username").fill("reviewer")
    page.locator("input[autocomplete=new-password]").nth(0).fill("short")
    page.locator("input[autocomplete=new-password]").nth(1).fill("short")
    page.get_by_role("button", name="Create account").click()

    expect(page.locator(".field-err")).to_have_text("password must have at least 8 characters")
    assert posts == []
