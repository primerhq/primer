"""API keys journey: admin key creation and table paging/filtering (Revamp 10, Task 6).

ui_e2e runs with auth off, so the signed-in user is the built-in "system" admin
(primer/api/middleware/auth.py:57-63), which is not a stored row; the admin
table (a fan-out over GET /v1/admin/users) never lists its keys, so the minted
key is checked through GET /v1/auth/tokens, and the table with stubbed reads.
"""

from __future__ import annotations

import json
import re
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

        page.get_by_test_id("create-token-btn").click()

        acts_as = page.get_by_test_id("at-acts-as")
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
