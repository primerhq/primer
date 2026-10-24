"""System Setup card journey (Revamp 10, Task 7)."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_view

pytestmark = [smk("SMK-UI-06", status="partial"), pytest.mark.ui_e2e]


def test_a_set_up_install_shows_a_green_setup_card(page: Page, console_url: str) -> None:
    wid = default_wid(console_url) or "primer"
    open_view(page, console_url, wid, "system:dashboard")
    card = page.get_by_test_id("nv-sys-setup")
    expect(card).to_be_visible(timeout=20_000)
    expect(card).to_have_attribute("data-tone", "ok")
    expect(page.get_by_test_id("nv-sys-setup-headline")).to_have_text("All 6 setup checks pass")


def test_a_failing_check_is_listed_with_its_fix_and_open_providers_goes_there(
    page: Page, console_url: str
) -> None:
    def handle_setup_state(route):
        if route.request.method != "GET":
            route.continue_()
            return
        state = {
            "predicates": [
                {
                    "key": "llm_provider",
                    "label": "LLM provider responds",
                    "ok": False,
                    "detail": "connection refused",
                },
                {"key": "model_profile", "label": "Model profile registered", "ok": True, "detail": None},
                {"key": "default_workspace", "label": "Default workspace reachable", "ok": True, "detail": None},
                {"key": "operator_agent", "label": "Operator agent seeded", "ok": False, "detail": "not found"},
                {"key": "builder_agent", "label": "Builder agent seeded", "ok": True, "detail": None},
                {"key": "system_collection", "label": "System collection exists", "ok": True, "detail": None},
            ]
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(state))

    page.route(re.compile(r".*/v1/setup/state(\?.*)?$"), handle_setup_state)

    wid = default_wid(console_url) or "primer"
    open_view(page, console_url, wid, "system:dashboard")

    card = page.get_by_test_id("nv-sys-setup")
    expect(card).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-sys-setup-headline")).to_have_text("2 of 6 setup checks fail")

    fix_llm = page.get_by_test_id("nv-sys-setup-fix:llm_provider")
    expect(fix_llm).to_have_text("Open Providers")
    fix_op = page.get_by_test_id("nv-sys-setup-fix:operator_agent")
    expect(fix_op).to_have_text("Re-run seed")

    fix_llm.click()
    expect(page).to_have_url(re.compile(r".*view=platform:providers.*"), timeout=15_000)


def test_re_run_seed_reports_errors_as_errors(page: Page, console_url: str) -> None:
    def handle_seed(route):
        if route.request.method != "POST":
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "created": [],
                    "skipped": [],
                    "errors": [["system_collection", "RuntimeError('boom')"]],
                }
            ),
        )

    page.route(re.compile(r".*/v1/setup/seed$"), handle_seed)

    wid = default_wid(console_url) or "primer"
    open_view(page, console_url, wid, "system:dashboard")

    page.get_by_test_id("nv-sys-setup-menu-btn").click()
    page.get_by_test_id("nv-sys-setup-rerun-seed").click()

    toast = page.get_by_test_id("nv-toasts-alert")
    expect(toast).to_contain_text("Seed finished with errors", timeout=15_000)


def test_reset_default_agents_asks_first_and_cancel_resets_nothing(
    page: Page, console_url: str
) -> None:
    resets_called = []

    def handle_reset(route):
        resets_called.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"reset": ["operator", "builder", "planner", "explorer", "tool-runner"]}),
        )

    page.route(re.compile(r".*/v1/setup/reset_agents$"), handle_reset)

    wid = default_wid(console_url) or "primer"
    open_view(page, console_url, wid, "system:dashboard")

    page.get_by_test_id("nv-sys-setup-menu-btn").click()
    page.get_by_test_id("nv-sys-setup-reset-agents").click()

    modal = page.locator(".modal-overlay")
    expect(modal).to_be_visible(timeout=10_000)
    expect(modal).to_contain_text("Reset default agents")
    expect(modal).to_contain_text("tool-runner")

    modal.get_by_role("button", name="Cancel").click()
    expect(modal).not_to_be_visible(timeout=10_000)

    assert len(resets_called) == 0


def test_the_setup_card_fits_a_phone(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    wid = default_wid(console_url) or "primer"
    page.goto(f"{console_url}#/w/{wid}?view=system:dashboard")
    card = page.get_by_test_id("nv-sys-setup")
    expect(card).to_be_visible(timeout=20_000)

    scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
    inner_width = page.evaluate("() => window.innerWidth")
    assert scroll_width <= inner_width


def test_reset_prime_instructions_asks_then_says_what_it_did(page: Page, console_url: str) -> None:
    """Pivot P2 Task 15: the admin's row (the ui_e2e server runs auth-disabled, as an admin) asks first, then says what the reset did. The POST is
    answered here, with an AGENTS.md beside the file, so the shared server's prime keeps its own agents.md."""
    posts = []

    def handle_reset(route):
        posts.append(route.request.method)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"workspace_id": wid, "path": "agents.md", "bytes": 4096, "shadows": "AGENTS.md"}),
        )

    page.route(re.compile(r".*/v1/setup/reset_prime_instructions$"), handle_reset)

    wid = default_wid(console_url) or "primer"
    open_view(page, console_url, wid, "system:dashboard")

    page.get_by_test_id("nv-sys-setup-menu-btn").click()
    row = page.get_by_test_id("nv-sys-setup-reset-prime")
    expect(row).to_have_attribute("data-verb", "setup.reset_prime_instructions")
    row.click()

    modal = page.locator(".modal-overlay")
    expect(modal).to_be_visible(timeout=10_000)
    expect(modal).to_contain_text("overwrites agents.md in the default workspace")
    modal.get_by_role("button", name="Reset instructions").click()

    expect(page.get_by_test_id("nv-sys-setup-prime-shadows")).to_contain_text("the loader now reads agents.md instead", timeout=15_000)
    expect(page.get_by_text(f"Prime instructions reset: agents.md in {wid} is the shipped version again.", exact=False).first).to_be_visible()
    assert posts == ["POST"]
