"""First-boot wizard: after step 2 the checklist says "checking", not a stale failure (console review C-039).

``SetupWizardGate`` held the setup state it had read BEFORE the wizard ran. When step 2 finished it flipped to the checklist and re-ran the seed, but kept drawing that old
state until the post-seed read came back, so for a moment the operator saw a red "LLM provider responds: no LLM provider configured" right after configuring one.

The setup API is the in-test fake of ``test_setup_wizard_resume_journey`` behind ``page.route`` (the shared install is already set up). A MutationObserver armed just before
"Finish setup" counts every DOM change after which the stale words are on the page, so the check does not depend on how long the fake takes to answer.
"""

from __future__ import annotations

from playwright.sync_api import expect

from tests.ui_e2e.test_setup_wizard_resume_journey import _FakeSetupApi, _connect_step_one, _open_wizard

STALE = "no LLM provider configured"

_ARM = """
() => {
  window.__stale = 0;
  new MutationObserver(() => {
    if (document.body.innerText.indexOf(%r) >= 0) window.__stale += 1;
  }).observe(document.body, { subtree: true, childList: true, characterData: true });
}
""" % STALE


def _to_step_two(page, console_url: str, api: _FakeSetupApi) -> None:
    _open_wizard(page, console_url, api)
    _connect_step_one(page)
    page.locator("#setup-model").wait_for(state="visible", timeout=10_000)


def test_finishing_step_two_never_shows_the_state_read_before_it(page, console_url: str) -> None:
    api = _FakeSetupApi()
    _to_step_two(page, console_url, api)
    page.evaluate(_ARM)

    page.get_by_role("button", name="Finish setup").click()

    expect(page.get_by_test_id("setup-gate-enter")).to_be_enabled(timeout=15_000)
    assert page.evaluate("() => window.__stale") == 0, "the checklist drew the pre-wizard state ('no LLM provider configured') after the provider was configured"
    assert api.complete


def test_a_failed_seed_leaves_the_true_state_and_a_way_to_re_run_it(page, console_url: str) -> None:
    api = _FakeSetupApi()
    _to_step_two(page, console_url, api)
    page.route("**/v1/setup/seed", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body='{"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "seed exploded"}',
    ))
    page.evaluate(_ARM)

    page.get_by_role("button", name="Finish setup").click()

    expect(page.locator(".auth-banner")).to_be_visible(timeout=15_000)   # the failure is shown (its wording is the gate's one banner, not this journey's subject)
    expect(page.get_by_test_id("setup-gate-predicate:llm_provider")).to_contain_text("LLM provider responds")
    expect(page.get_by_test_id("setup-gate-predicate:llm_provider")).not_to_contain_text(STALE)
    expect(page.get_by_test_id("setup-gate-predicate-fix:operator_agent")).to_be_visible()
    assert page.evaluate("() => window.__stale") == 0
    expect(page.get_by_test_id("setup-gate-enter")).to_be_disabled()
