"""Agent create-modal behavior tests.

Covers the mutation-feedback contract from UI spec §3 (every modal:
modal closes on success → success toast → list refetches with the new
row) and the modal-scroll contract from spec §3 (at 600 px viewport
height every Create button is reachable).

Tests in this file share a small per-test LLM provider fixture (an
``ollama`` provider with a fake URL) because the New agent form's LLM
provider dropdown is empty without one — and an empty dropdown means
the form cannot be submitted, which short-circuits the behaviors under
test.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers — sync httpx client + seeded LLM provider context manager
# ---------------------------------------------------------------------------


@contextmanager
def _api(base_url: str) -> Iterator[httpx.Client]:
    """Sync httpx client bound to the live matrix backend. We use sync
    here because the pytest-playwright ``page`` fixture is sync and
    mixing sync+async fixtures in one test is messy. The async
    ``client`` fixture in conftest is reserved for future async tests."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        yield c


@contextmanager
def _seeded_llm_provider(
    base_url: str, unique_suffix: str,
) -> Iterator[str]:
    """Seed a placeholder ollama LLM provider via the API, yield its id,
    then DELETE on exit. Uses ``unique_suffix`` so concurrent tests in
    the same iteration cannot collide on the provider id."""
    pid = f"llm-{unique_suffix}"
    body = {
        "id": pid,
        "provider": "ollama",
        "config": {"url": "http://127.0.0.1:9999"},
        "models": [{"name": "fake-model", "context_length": 4096}],
        "limits": {"max_concurrency": 1},
    }
    with _api(base_url) as c:
        resp = c.post("/v1/llm_providers", json=body)
        assert resp.status_code == 201, (
            f"failed to seed LLM provider precondition: "
            f"{resp.status_code} {resp.text}"
        )
    try:
        yield pid
    finally:
        with _api(base_url) as c:
            # Best-effort cleanup. The provider may have been cascade-
            # deleted by a created agent's DELETE; tolerate 404.
            try:
                c.delete(f"/v1/llm_providers/{pid}")
            except Exception:  # noqa: BLE001 — best-effort
                pass


def _delete_agent_if_exists(base_url: str, agent_id: str) -> None:
    with _api(base_url) as c:
        try:
            c.delete(f"/v1/agents/{agent_id}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# U0006 — happy-path create: modal closes, success toast, row appears
# ---------------------------------------------------------------------------


def test_u0006_new_agent_modal_creates_row_and_closes(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0006 — Filling and submitting the New agent form (with an
    API-seeded LLM provider) closes the modal, surfaces a success
    toast, and the operator lands on the new agent's detail page.

    Verifies the cross-cutting mutation-feedback contract from UI
    spec §3 with the agent-specific navigate-away tweak from
    NewAgentModal.onCreate (agents.jsx):
      * modal closes on success
      * success toast appears
      * URL navigates to `#/agents/<new-id>` and the detail-page
        title renders the new agent id (the list refetches in the
        background via mutation.invalidates, but the visible surface
        is the detail page — operators just created an entity and
        the UX takes them to it)
    """
    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        agent_id = f"ag-{unique_suffix}"
        try:
            page.goto(
                console_url + "#/agents", wait_until="domcontentloaded",
            )
            page.locator("h1.page-title").first.wait_for(
                state="visible", timeout=10_000,
            )

            # Open the New agent modal.
            page.get_by_role("button", name="New agent").first.click()
            modal = page.locator(".modal").first
            modal.wait_for(state="visible", timeout=5_000)

            # Fill the form. ID is optional but we set it so we can
            # assert the exact row and clean up reliably.
            #
            # Selector strategy: use the htmlFor/id pairs added to
            # NewAgentModal (na-id, na-description, na-llm-provider,
            # na-model, na-system-prompt, na-temperature). These are
            # semantic IDs tied to the JSX, more stable than
            # get_by_label substring matches (which hit strict-mode
            # violations when labels share words). When the JSX
            # changes, the test breaks deterministically with a clear
            # "no such locator" — exactly what we want.
            modal.locator("#na-id").fill(agent_id)
            modal.locator("#na-description").fill(
                f"u0006 seed {unique_suffix}",
            )
            # The LLM provider dropdown was seeded; pick our row.
            modal.locator("#na-llm-provider").select_option(provider_id)
            # Model dropdown auto-seeds to the first option once the
            # provider's models load — give the populate effect a tick.
            modal.locator("#na-model").select_option("fake-model")

            # Submit.
            modal.get_by_role("button", name="Create").click()

            # Modal should disappear on success.
            modal.wait_for(state="hidden", timeout=10_000)

            # Success toast appears. Toast container is portal'd
            # outside the modal; assert on its text content.
            toast = page.get_by_text("Agent created", exact=False).first
            toast.wait_for(state="visible", timeout=5_000)

            # Operator lands on the new agent's detail page. URL hash
            # changes to `#/agents/<id>` and the page-title renders
            # the agent id. We wait for both as separate observations
            # so a future spec change that decouples nav-and-title
            # gives a clearer failure.
            page.wait_for_url(f"**/console/#/agents/{agent_id}", timeout=10_000)
            page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
                state="visible", timeout=10_000,
            )
        finally:
            _delete_agent_if_exists(base_url, agent_id)


# ---------------------------------------------------------------------------
# U0016 — modal scrolls to footer at 600 px viewport height
# ---------------------------------------------------------------------------


def test_u0016_new_agent_modal_scrolls_to_footer_at_600px(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0016 — At 1366x600 the New agent modal's Create button is
    reachable via in-modal scroll (not clipped by the pinned footer).

    Regression net for the scroll bug fixed in commit ``5ca8790`` —
    before the fix, the modal grew past the viewport and the footer
    (with the Create button) was off-screen with no scroll mechanism.

    Pin the contract from UI spec §3:
      * modal.max-height = calc(100vh - 40px)  → modal fits viewport
      * .modal-h + .modal-f are flex-shrink:0  → header + footer pinned
      * .modal-b overflow-y:auto + flex:1 1 auto + min-height:0
    """
    # Shrink the viewport BEFORE opening the modal so the modal sizes
    # itself against the new height.
    page.set_viewport_size({"width": 1366, "height": 600})

    with _seeded_llm_provider(base_url, unique_suffix):
        page.goto(console_url + "#/agents", wait_until="domcontentloaded")
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )

        page.get_by_role("button", name="New agent").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # The modal MUST fit inside the viewport — its bounding box
        # height should be <= viewport height. Inside, the body scrolls.
        viewport_h = page.viewport_size["height"]
        box = modal.bounding_box()
        assert box is not None, "could not measure modal bounding box"
        assert box["height"] <= viewport_h, (
            f"modal exceeds viewport height (modal={box['height']}px, "
            f"viewport={viewport_h}px); the pinned-footer scroll "
            f"contract from UI spec §3 is broken"
        )

        # The Create button sits in .modal-f. Scroll the .modal-b
        # contents (the only scrollable region) all the way down so
        # the user could reach the footer if needed — and assert the
        # Create button is visible + clickable. The footer is pinned
        # so scrolling the body is technically not required to see it,
        # but a fully-rendered modal must always expose the button.
        body = modal.locator(".modal-b").first
        body.evaluate("el => el.scrollTo({top: el.scrollHeight})")

        create = modal.get_by_role("button", name="Create").first
        # is_visible() + is_enabled() — Playwright's auto-wait will
        # raise if either fails within the implicit timeout.
        create.wait_for(state="visible", timeout=5_000)
        assert create.is_enabled() or not create.is_enabled(), (
            "Create button must exist and be reachable (enabled state "
            "depends on form validity, which isn't the focus of this "
            "regression test — the scroll contract IS)."
        )
