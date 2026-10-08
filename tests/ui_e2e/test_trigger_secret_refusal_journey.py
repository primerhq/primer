"""Journey: a refused webhook-secret write says so, with the server's reason (follow-up to PR 491).

A caller who is not the trigger's owner or an admin is refused with a 403 whose detail explains the rule. Clear HMAC used to swallow that
(the secret stayed, nothing was shown), and Rotate token and the Set HMAC dialog showed only "Forbidden". The refusal is answered by the test
(auth is off in this lane, so there is no second user to be refused by); the trigger is real.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_legacy_route

REASON = "Only the trigger's owner or an admin may change a webhook trigger's secrets."
_PROBLEM = {"type": "/errors/forbidden-role", "title": "Forbidden", "status": 403, "detail": REASON}


@pytest.fixture
def webhook(base_url: str):
    slug = f"hmac-refusal-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "config": {"kind": "webhook", "hmac_secret": "s3cret"}, "enabled": True})
        assert r.status_code == 201, r.text
        trigger_id = r.json()["id"]
    try:
        yield trigger_id
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/triggers/{trigger_id}")


def _refuse(page: Page, trigger_id: str) -> list[str]:
    seen: list[str] = []

    def refuse(route) -> None:
        seen.append(route.request.method + " " + route.request.url.split("/v1/", 1)[1])
        route.fulfill(status=403, content_type="application/problem+json", body=json.dumps(_PROBLEM))

    page.route(f"**/v1/triggers/{trigger_id}", lambda route: refuse(route) if route.request.method == "PUT" else route.continue_())
    page.route(f"**/v1/triggers/{trigger_id}/rotate_token", refuse)
    return seen


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_clear_hmac_says_why_it_was_refused_and_the_secret_stays(page: Page, console_url: str, webhook: str) -> None:
    seen = _refuse(page, webhook)
    open_legacy_route(page, console_url, f"triggers/{webhook}")
    expect(page.get_by_test_id("clear-hmac-btn")).to_be_visible(timeout=30_000)

    page.get_by_test_id("clear-hmac-btn").click()
    page.get_by_test_id("dialog-confirm").click()

    error = page.get_by_test_id("hmac-error")
    expect(error).to_be_visible(timeout=10_000)
    expect(error).to_contain_text(REASON)
    assert any(s.startswith("PUT triggers/") for s in seen), "the write was attempted and refused"
    expect(page.get_by_test_id("clear-hmac-btn")).to_be_visible()      # the secret is still configured

    # The next attempt starts clean: the stale message goes while it runs.
    page.get_by_test_id("clear-hmac-btn").click()
    page.get_by_test_id("dialog-confirm").click()
    expect(error).to_contain_text(REASON, timeout=10_000)


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_rotate_token_and_the_set_dialog_show_the_servers_reason_not_just_its_title(page: Page, console_url: str, webhook: str) -> None:
    _refuse(page, webhook)
    open_legacy_route(page, console_url, f"triggers/{webhook}")
    expect(page.get_by_test_id("rotate-token-btn")).to_be_visible(timeout=30_000)

    page.get_by_test_id("rotate-token-btn").click()
    page.get_by_test_id("dialog-confirm").click()
    expect(page.get_by_text(REASON)).to_be_visible(timeout=10_000)

    page.get_by_test_id("set-hmac-btn").click()
    page.get_by_test_id("tr-hmac-secret-input").fill("another-secret")
    page.get_by_role("button", name="Save secret").click()
    expect(page.get_by_test_id("tr-hmac-dialog").get_by_text(REASON)).to_be_visible(timeout=10_000)
