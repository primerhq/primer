"""Journey: a failed status read shows an error with a retry, never "Create the operator account" (console review follow-up).

The status read fails as a dropped connection, then as a 503, then answers. The gate must say what failed, never show the register form,
and recover when the server answers. The mode is switched by the test (the gate also retries on its own every few seconds, so counting
reads would race it).
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_gate


@pytest.mark.timeout(120)
def test_a_failed_status_read_is_an_error_with_a_retry_and_never_a_register_form(page: Page, console_url: str) -> None:
    mode = {"v": "refuse"}
    reads = {"n": 0}

    def handler(route) -> None:
        reads["n"] += 1
        if mode["v"] == "refuse":
            route.abort("connectionrefused")
        elif mode["v"] == "unavailable":
            route.fulfill(status=503, content_type="application/problem+json", body=json.dumps({
                "type": "/errors/service-unavailable", "title": "Service Unavailable", "status": 503, "detail": "starting up",
            }))
        else:
            route.continue_()

    page.route("**/v1/auth/status", handler)
    open_gate(page, console_url)

    failure = page.get_by_test_id("auth-unreachable")
    expect(failure).to_have_text("Cannot reach the server", timeout=15_000)
    expect(page.get_by_text("Create the operator account")).to_have_count(0)

    mode["v"] = "unavailable"
    page.get_by_test_id("auth-retry").click()
    expect(failure).to_have_text("The server is not ready", timeout=10_000)
    expect(page.get_by_text("Create the operator account")).to_have_count(0)

    mode["v"] = "ok"
    page.get_by_test_id("auth-retry").click()
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("auth-unreachable")).to_have_count(0)
    assert reads["n"] >= 3
