"""API keys journey: admin key creation and table paging/filtering (Revamp 10, Task 6).

ui_e2e runs with auth off, so the signed-in user is the built-in "system" admin
(primer/api/middleware/auth.py:57-63), which is not a stored row; the admin
table (a fan-out over GET /v1/admin/users) never lists its keys, so the minted
key is checked through GET /v1/auth/tokens, and the table with stubbed reads.
"""

from __future__ import annotations

import json
import re
import time
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_view

pytestmark = smk("SMK-UI-03", status="partial")


def test_an_admin_mints_a_key_that_acts_as_them(page: Page, base_url: str, console_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        status_res = client.get("/v1/auth/status")
        username = status_res.json().get("username") or "system"

    key_name = f"e2e-key-{uuid.uuid4().hex[:8]}"

    try:
        expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
        wid = default_wid(console_url) or "primer"
        open_view(page, console_url, wid, "system:apikeys")

        # Settle the console before driving the dialog. Two load-sensitive
        # vectors (lead forensics 2026-10-22; the reds at #833, #837 and the
        # #839 red at f4a883da0):
        #
        # 1. The shell's "auth-status" resource is a one-shot fetch (pollMs 0,
        #    no retry) shared across views. Under load the 200 response to
        #    GET /v1/auth/status can be superseded or aborted in
        #    use-resource.js, so the context username stays pinned to the
        #    "anon" fallback for the page's whole life even though the server
        #    answered 200 (nv-shell.jsx:655). The reload below gives it a
        #    fresh attempt, and the gate waits for that attempt to actually
        #    land (the shell avatar, nv-profile-btn, renders the first two
        #    chars of the same context username the acts-as line reads)
        #    before driving the dialog; if it does not settle within a grace
        #    the test reloads for a fresh attempt (the hash URL survives).
        # 2. The boot can remount the view subtree after a fast click and
        #    lose the dialog state, so the click never lands in a live tree;
        #    a bounded re-click lands on the settled tree.
        page.reload()
        expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)

        expected_initials = username[:2].lower()
        settled = False
        for _doc in range(5):
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                try:
                    initials = (
                        page.get_by_test_id("nv-profile-btn")
                        .inner_text(timeout=1_500)
                        .strip()
                    )
                except Exception:
                    initials = ""
                if initials == expected_initials:
                    settled = True
                    break
                page.wait_for_timeout(250)
            if settled:
                break
            page.reload()
            expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)

        acts_as = page.get_by_test_id("at-acts-as")
        for _click in range(3):
            page.get_by_test_id("create-token-btn").click()
            try:
                expect(acts_as).to_be_visible(timeout=5_000)
                break
            except AssertionError:
                continue
        expect(acts_as).to_have_text(f"This key acts as you, {username}.")

        page.get_by_test_id("at-name").fill(key_name)
        page.get_by_test_id("create-token-submit").click()

        expect(page.get_by_test_id("at-plaintext-dialog")).to_be_visible(timeout=15_000)
        page.get_by_test_id("close-plaintext-btn").click()
        expect(page.get_by_test_id("at-plaintext-dialog")).not_to_be_visible(timeout=15_000)

        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            tokens_res = client.get("/v1/auth/tokens")
            items = tokens_res.json().get("items", [])
            matched = [t for t in items if t.get("name") == key_name]
            assert len(matched) == 1, f"expected 1 token named {key_name}, got {items}"
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            tokens_res = client.get("/v1/auth/tokens")
            items = tokens_res.json().get("items", [])
            for t in items:
                if t.get("name") == key_name:
                    client.delete(f"/v1/auth/tokens/{t['id']}")


def test_the_admin_table_filters_and_pages_with_an_owner_column(page: Page, console_url: str) -> None:
    def handle_users(route):
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": [{"id": "u1", "username": "bo"}]}),
        )

    def handle_tokens(route):
        if route.request.method != "GET":
            route.continue_()
            return
        rows = [
            {
                "id": f"tok-{i:02d}",
                "name": f"tok-{i:02d}",
                "prefix": f"pk_{i:02d}",
                "scopes": [],
                "status": "active",
            }
            for i in range(25)
        ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": rows}),
        )

    page.route(re.compile(r".*/v1/admin/users(\?.*)?$"), handle_users)
    page.route(re.compile(r".*/v1/admin/users/u1/tokens$"), handle_tokens)

    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    wid = default_wid(console_url) or "primer"
    open_view(page, console_url, wid, "system:apikeys")

    admin_page = page.get_by_test_id("at-page-admin")
    expect(admin_page).to_be_visible(timeout=20_000)
    expect(admin_page.get_by_text("Owner")).to_be_visible()
    expect(admin_page.locator('[data-testid^="api-token-row-"]')).to_have_count(20)

    pager = admin_page.get_by_test_id("pk-pager")
    expect(pager).to_be_visible()
    last_btn = pager.locator("button").last
    last_btn.click()
    expect(admin_page.locator('[data-testid^="api-token-row-"]')).to_have_count(5)

    filter_input = admin_page.get_by_test_id("at-filter")
    filter_input.fill("tok-07")
    expect(admin_page.locator('[data-testid^="api-token-row-"]')).to_have_count(1)


def test_the_profile_lists_your_api_keys_with_one_plus_api_key(page: Page, console_url: str) -> None:
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    wid = default_wid(console_url) or "primer"
    open_view(page, console_url, wid, "system:profile")

    personal_page = page.get_by_test_id("at-page-personal")
    expect(personal_page).to_be_visible(timeout=20_000)
    expect(personal_page.get_by_text("Your API keys")).to_be_visible()
    expect(personal_page.get_by_role("button", name="+ API key")).to_have_count(1)
