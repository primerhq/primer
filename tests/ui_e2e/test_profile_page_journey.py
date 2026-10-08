"""System > Profile: a wrong current password says so, and the empty states describe what is there (ADM-25 and ADM-27 of the 2026-10-08 admin review).

The wrong-password request is REAL (``POST /v1/auth/change-password`` answers 401 ``invalid_credentials`` and the page used to print that code as it came). Three READS are
stubbed so the page's contents do not depend on what a shared instance holds: the personal token list and the linked identities (empty, so both empty states show) and the
SSO providers (none, or one, to show both wordings of the linked-accounts hint). Nothing is written: a wrong password changes nothing.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _stub_reads(page: Page, providers: list[dict]) -> None:
    def reply(body):
        def handler(route):
            if route.request.method != "GET":
                route.continue_()
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        return handler

    page.route(re.compile(r".*/v1/auth/tokens$"), reply({"items": [], "total": 0}))
    page.route(re.compile(r".*/v1/auth/sso/identities$"), reply([]))
    page.route(re.compile(r".*/v1/auth/sso/providers$"), reply(providers))


def _open_profile(page: Page, console_url: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation), so wait for it and
    navigate once more if the Profile page is not there after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-sys-profile")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", "system:profile")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def test_a_wrong_current_password_is_told_in_words_and_the_empty_states_describe_what_is_there(page: Page, console_url: str) -> None:
    _stub_reads(page, providers=[])
    _open_profile(page, console_url)

    page.get_by_test_id("nv-pw-current").fill("definitely-not-the-password-1")
    page.get_by_test_id("nv-pw-next").fill("a-long-enough-new-one-1")
    page.get_by_test_id("nv-pw-submit").click()

    error = page.locator(".nv-form-error")
    expect(error).to_contain_text("The current password is not right.", timeout=15_000)
    assert "invalid_credentials" not in error.inner_text(), error.inner_text()

    # ADM-27: with no SSO provider configured there is nothing "below" to link, and the page says where one is added.
    empty = page.get_by_test_id("linked-accounts-empty")
    expect(empty).to_contain_text("No single sign-on provider is configured on this server", timeout=15_000)
    expect(empty).to_contain_text("System > SSO")
    assert "below" not in empty.inner_text(), empty.inner_text()

    # One "Create token" on the page, the header's: the empty state does not repeat it.
    expect(page.get_by_test_id("nv-sys-profile").get_by_role("button", name="Create token")).to_have_count(1)


def test_with_a_provider_configured_the_linked_accounts_hint_points_at_the_list_below(page: Page, console_url: str) -> None:
    _stub_reads(page, providers=[{"id": "okta", "name": "Okta"}])
    _open_profile(page, console_url)

    expect(page.get_by_test_id("linked-accounts-empty")).to_contain_text("Link a single sign-on provider below", timeout=15_000)
    expect(page.get_by_test_id("link-provider-btn-okta")).to_be_visible()
