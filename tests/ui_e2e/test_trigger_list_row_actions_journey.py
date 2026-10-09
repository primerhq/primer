"""Journey: the triggers list row says when Fire now or Delete is refused (board ticket 01a11db6-44fd).

The legacy triggers list (``?overlay=triggers``) draws ``TR_TriggerRow``. Its Fire now and Delete swallowed the server's refusal and left the row as it was. With the write answered by the
body the REAL auth gate and error handlers produce (``tests/_support/trigger_envelopes.py``; this scratch server runs with auth off, so the browser is given the answer), a toast now says
"Fire failed" / "Delete failed" with the reader's sentence for an ended session, the buttons come back, and the row is still there; a Fire now that goes through says so.

Round 2 (lead's review of #695), against the REAL server: a Fire now on a DISABLED trigger is a 200 ``{skipped: true}`` and says "Not fired" (not "Trigger fired"); a Fire now on a trigger that was
deleted behind the list's back is a 404 ``trigger_not_found`` and refreshes the list, so the stale row goes.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests._support.smk import smk
from tests._support.trigger_envelopes import real_envelopes
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")


@pytest.mark.ui_e2e
def test_a_refused_fire_now_and_delete_on_the_list_row_are_said(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"row-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "webhook"}, "enabled": True})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
    ended = real_envelopes()["session_ended"]
    ended = {**ended, "extensions": {**ended["extensions"], "request_id": "req-journey-1"}}
    refuse = {"on": True}

    def answer(route: Route) -> None:
        if refuse["on"]:
            route.fulfill(status=401, content_type="application/problem+json", body=json.dumps(ended))
        else:
            route.continue_()

    page.route(f"**/v1/triggers/{tid}/fire_now", answer)
    page.route(f"**/v1/triggers/{tid}", lambda route: answer(route) if route.request.method == "DELETE" else route.continue_())
    try:
        open_legacy_route(page, console_url, "triggers")
        row = page.get_by_test_id(f"trigger-row-{tid}")
        expect(row).to_be_visible(timeout=20_000)

        page.get_by_test_id(f"trigger-row-fire-{tid}").click()
        alert = page.get_by_test_id("nv-toasts-alert")
        expect(alert).to_contain_text("Fire failed", timeout=10_000)
        expect(alert).to_contain_text("sign in again")
        expect(alert).not_to_contain_text("auth_required")
        expect(alert).to_contain_text("req-journey-1")
        expect(page.get_by_test_id("toast-copy-request-id")).to_be_visible()
        expect(page.get_by_test_id(f"trigger-row-fire-{tid}")).to_be_enabled()

        page.get_by_test_id(f"trigger-row-delete-{tid}").click()
        page.get_by_test_id("dialog-confirm").click()
        expect(alert).to_contain_text("Delete failed", timeout=10_000)
        expect(row).to_be_visible()

        # a fire that goes through says so
        refuse["on"] = False
        page.get_by_test_id(f"trigger-row-fire-{tid}").click()
        expect(page.get_by_test_id("nv-toasts-status")).to_contain_text("Trigger fired", timeout=10_000)
    finally:
        page.unroute(f"**/v1/triggers/{tid}/fire_now")
        page.unroute(f"**/v1/triggers/{tid}")
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/triggers/{tid}")


@pytest.mark.ui_e2e
def test_a_fire_now_on_a_disabled_trigger_says_it_was_not_fired(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"off-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "webhook"}, "enabled": False})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
    try:
        open_legacy_route(page, console_url, "triggers")
        expect(page.get_by_test_id(f"trigger-row-{tid}")).to_be_visible(timeout=20_000)
        page.get_by_test_id(f"trigger-row-fire-{tid}").click()
        status = page.get_by_test_id("nv-toasts-status")
        expect(status).to_contain_text("Not fired", timeout=10_000)
        expect(status).to_contain_text(f"{slug} is disabled; enable it to fire it.")
        expect(status).not_to_contain_text("Trigger fired")
        expect(page.get_by_test_id(f"trigger-row-fire-{tid}")).to_be_enabled()
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/triggers/{tid}")


@pytest.mark.ui_e2e
def test_a_fire_now_on_a_trigger_deleted_behind_the_lists_back_refreshes_the_list(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"gone-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "webhook"}, "enabled": True})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
    try:
        open_legacy_route(page, console_url, "triggers")
        row = page.get_by_test_id(f"trigger-row-{tid}")
        expect(row).to_be_visible(timeout=20_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.delete(f"/v1/triggers/{tid}").status_code in (200, 204)    # gone, and the list does not know
        page.get_by_test_id(f"trigger-row-fire-{tid}").click()
        alert = page.get_by_test_id("nv-toasts-alert")
        expect(alert).to_contain_text(f"{slug} no longer exists; the list has been refreshed.", timeout=10_000)
        expect(alert).not_to_contain_text("Go back")
        expect(row).to_have_count(0, timeout=10_000)                            # the stale row is gone
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/triggers/{tid}")
