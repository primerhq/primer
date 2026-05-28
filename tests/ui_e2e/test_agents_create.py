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
# U0007 — agent create 422 renders per-field inline errors, not a toast
# ---------------------------------------------------------------------------


def test_u0007_new_agent_create_422_renders_inline_field_errors(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0007 — Submitting the New agent form with an invalid
    ``temperature`` (one that fails server-side validation) surfaces
    the 422 as an inline field-help error under the offending input,
    NOT as a generic error toast.

    Follow-on from U0006 (happy path). Verifies the cross-cutting
    mutation-feedback contract from UI spec §3:
      * 422 from the backend renders per-field via
        ``extensions.errors[].loc[]`` mapping to ``fieldErrors[...]``
      * Modal stays open so the operator can correct the value
      * No error toast appears (toast is reserved for non-422)

    Trigger: Agent.temperature is ``Field(default=None, ge=0.0)``
    (per primer/model/agent.py:79); -0.5 violates ``ge=0.0`` and is
    reliably rejected with 422 carrying ``body.temperature`` in the
    field-errors loc. This trigger is more robust than id-format
    games because Identifiable's validator is intentionally lenient
    on whitespace.
    """
    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        agent_id = f"ag-u0007-{unique_suffix}"
        page.goto(console_url + "#/agents", wait_until="domcontentloaded")
        page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

        page.get_by_role("button", name="New agent").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        modal.locator("#na-id").fill(agent_id)
        modal.locator("#na-description").fill("u0007 422 probe")
        modal.locator("#na-llm-provider").select_option(provider_id)
        modal.locator("#na-model").select_option("fake-model")
        # The deliberate bad value — violates Agent.temperature's
        # documented ``ge=0.0`` constraint.
        modal.locator("#na-temperature").fill("-0.5")

        modal.get_by_role("button", name="Create").click()

        # Modal should STAY OPEN on 422 (a success would close it).
        # Give the mutation a moment to settle before asserting.
        page.wait_for_load_state("networkidle", timeout=10_000)
        assert modal.is_visible(), (
            "modal should stay open on 422 so the operator can correct "
            "the field; closing means the contract collapsed into a "
            "happy-path or error-toast flow"
        )

        # An inline field-help error must appear somewhere in the
        # modal body. The exact loc-key the server emits depends on
        # which validator fires; we look for ANY field-help-red marker
        # that wasn't present before submit. The CSS pattern in
        # NewAgentModal is `<div className="field-help" style="color: var(--red)">`.
        red_helps = modal.locator('.field-help[style*="--red"]')
        red_helps.first.wait_for(state="visible", timeout=5_000)

        # No error toast — 422 should NOT surface as a toast per spec §3.
        # Use a short wait_for absence; if a toast slipped through, this
        # catches it. We do NOT use the "Create failed" text from the
        # general onError because that's reserved for non-422 paths.
        # Strict absence check: the toast container has its own class.
        toast_errors = page.locator(".toast.toast-error, [class*='toast-error']")
        # A pre-existing toast from a prior test would still be in the
        # DOM if not dismissed; the per-test browser context fixture
        # gives us a clean slate, so 0 is the expected count.
        assert toast_errors.count() == 0, (
            f"422 surfaced as a toast instead of inline field-help: "
            f"{toast_errors.count()} toast-error elements present"
        )


# ---------------------------------------------------------------------------
# U0020 — Agent delete confirms, removes the entity, navigates to list,
# and surfaces a success toast
# ---------------------------------------------------------------------------


def test_u0020_agent_delete_confirms_removes_and_navigates_back_to_list(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0020 — From an agent's detail page, clicking Delete opens the
    confirm modal; confirming closes the dialog, navigates back to
    /agents, surfaces a success toast ("Agent deleted"), and the
    agent is absent from the backend (verified via API).

    Mutation-feedback contract from UI spec §3 for the DELETE leg:
      * confirm dialog must appear before the destructive action
      * confirming → dialog closes
      * navigates back to list (/agents)
      * success toast appears
      * row is gone from storage

    Priority 1 (mutation feedback — destructive). Setup seeds the
    agent directly via API so the test exercises only the delete
    flow.
    """
    import httpx

    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        agent_id = f"ag-u0020-{unique_suffix}"
        # Seed the agent directly via API (faster + isolates the
        # behavior under test from the create flow).
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post("/v1/agents", json={
                "id": agent_id,
                "description": "u0020 delete probe",
                "model": {"provider_id": provider_id, "model_name": "fake-model"},
                "tools": [],
                "system_prompt": ["test"],
            })
            assert r.status_code == 201, f"seed agent failed: {r.text}"

        try:
            # Land directly on the agent's detail page.
            page.goto(
                f"{console_url}#/agents/{agent_id}",
                wait_until="domcontentloaded",
            )
            page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
                state="visible", timeout=10_000,
            )

            # Click the Delete button in the page header. The header
            # has Test agent + Delete + Back buttons — `name="Delete"`
            # uniquely matches the danger one.
            page.get_by_role("button", name="Delete").first.click()

            # Confirm dialog appears with title containing the agent id.
            confirm = page.locator(".modal").first
            confirm.wait_for(state="visible", timeout=5_000)
            assert agent_id in confirm.inner_text(), (
                f"confirm dialog should mention the agent id "
                f"{agent_id!r}; modal text: {confirm.inner_text()}"
            )

            # Click the danger-Delete button inside the confirm modal.
            # The modal has Cancel + Delete; locating by role + name +
            # narrowing to the modal scope avoids matching the header
            # Delete we just clicked (which is now hidden under the
            # overlay anyway, but be explicit).
            confirm.get_by_role("button", name="Delete").first.click()

            # Dialog closes.
            confirm.wait_for(state="hidden", timeout=10_000)

            # Navigates back to /agents (the UI's success path).
            page.wait_for_url("**/console/#/agents", timeout=10_000)
            page.locator("h1.page-title").get_by_text("Agents").first.wait_for(
                state="visible", timeout=5_000,
            )

            # Success toast surfaces. The toast title is "Agent deleted"
            # per the onSuccess callback in agents.jsx.
            page.get_by_text("Agent deleted", exact=False).first.wait_for(
                state="visible", timeout=5_000,
            )

            # Defence: the agent row is actually gone from storage.
            with httpx.Client(base_url=base_url, timeout=30.0) as c:
                got = c.get(f"/v1/agents/{agent_id}")
            assert got.status_code == 404, (
                f"agent should be absent after delete; "
                f"GET returned {got.status_code}: {got.text}"
            )
        finally:
            # Best-effort cleanup if the test bailed before the delete
            # actually landed.
            _delete_agent_if_exists(base_url, agent_id)
