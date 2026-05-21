"""Anomaly-helper-text regression tests for the provider create modals.

Each documented anomaly that has a UI surface in the LLM provider create
form gets a tiny assert-only test here. No backend precondition needed
— the helper text is in the JSX and renders whenever the modal opens.

Currently covers:
* U0010 — T0025 static-models helper under the models table.

Future tests (T0379, etc.) belong in this same file under the same
shape so the loop's generator + picker treat them as a cohesive batch.
"""

from __future__ import annotations


def test_u0010_llm_provider_modal_shows_t0025_static_models_helper(
    page,
    console_url: str,
) -> None:
    """U0010 — Opening the New LLM provider modal renders the documented
    T0025 helper text ("Model list comes from the provider row, not a
    live introspection (T0025)") under the models table.

    The text is unconditional UI copy in PROVIDER_FIELDS' Models section
    — no backend precondition required. We assert both the human-readable
    phrasing and the (T0025) tag are present so a future copy-edit can't
    silently drop the anomaly reference.
    """
    page.goto(console_url + "#/providers/llm", wait_until="domcontentloaded")
    page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

    # Open the New LLM provider modal. The button label is
    # "New llm provider" (lowercased label per providers.jsx render).
    new_btn = page.get_by_role("button", name="New llm provider").first
    new_btn.wait_for(state="visible", timeout=5_000)
    new_btn.click()

    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    # The helper text lives in the modal body. Use a substring match so a
    # punctuation tweak doesn't break the test, but pin the key phrase.
    modal_text = modal.inner_text()
    assert "Model list comes from the provider row" in modal_text, (
        "Expected T0025 helper text inside the New LLM provider modal; "
        "modal body did not contain the documented phrasing.\n"
        f"Modal text was:\n{modal_text}"
    )
    assert "T0025" in modal_text, (
        "T0025 anomaly tag missing from the modal body — copy edit "
        "dropped the anomaly reference?\nModal text was:\n" + modal_text
    )
