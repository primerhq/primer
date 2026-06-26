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


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-02")


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


def test_u0011_llm_provider_modal_shows_t0379_cross_validation_warning(
    page,
    console_url: str,
) -> None:
    """U0011 — Opening the New LLM provider modal renders the documented
    T0379 cross-validation warning ("Provider ↔ config alignment is NOT
    cross-validated server-side (T0379) — make sure the vendor name
    matches the config shape") somewhere in the modal body.

    Sister of U0010. The T0379 warning lives alongside the provider
    dropdown in PROVIDER_FIELDS so operators see it before submitting
    a misaligned provider×config combo. Like U0010, no backend
    precondition needed — text is unconditional UI copy.

    Defence: if a future copy-edit drops the T0379 reference, this
    test catches it before the anomaly drift propagates to operators.
    """
    page.goto(console_url + "#/providers/llm", wait_until="domcontentloaded")
    page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

    page.get_by_role("button", name="New llm provider").first.click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    modal_text = modal.inner_text()
    assert "Provider" in modal_text and "config" in modal_text, (
        "Expected T0379 cross-validation warning copy mentioning "
        "'Provider ↔ config' alignment in the New LLM provider modal.\n"
        f"Modal text was:\n{modal_text}"
    )
    assert "cross-validated" in modal_text or "cross-validation" in modal_text, (
        "Expected T0379 helper text mentioning 'cross-validated' / "
        "'cross-validation' in the modal body — copy drift?\n"
        f"Modal text was:\n{modal_text}"
    )
    assert "T0379" in modal_text, (
        "T0379 anomaly tag missing from the modal body — copy edit "
        "dropped the anomaly reference?\nModal text was:\n" + modal_text
    )


def test_provider_create_disabled_until_model_name_filled(
    page,
    console_url: str,
) -> None:
    """Regression: the New provider modal's Create button must stay
    disabled until every model row has its required fields filled.

    Pre-fix, ``canSubmit`` only checked ``models.length > 0``. The
    ``Add`` button seeds a row with blank fields, so adding a row and
    leaving the name empty enabled Create; submitting then sent
    ``models: [{}]``, which the API rejects with 422
    ``body.models.0.name: Field required``. The fix requires every
    non-optional model field to be non-empty before enabling Create.

    Uses the embedding provider modal (single ``name`` model field, no
    backend precondition): fill the required Base URL, add an empty model
    row, assert Create is disabled, then type a model name and assert it
    becomes enabled.
    """
    from playwright.sync_api import expect

    page.goto(
        console_url + "#/providers/embedding", wait_until="domcontentloaded"
    )
    page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

    page.get_by_role("button", name="New embedding provider").first.click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    create_btn = modal.get_by_role("button", name="Create", exact=True)

    # Fill the required Base URL so only the empty model row gates submit.
    modal.get_by_placeholder("https://api.openai.com/v1").fill(
        "http://localhost:1234/v1"
    )

    # Add a model row but leave its name blank — the regression case.
    modal.get_by_role("button", name="Add", exact=True).click()
    expect(modal.get_by_placeholder("Model name")).to_be_visible()
    expect(create_btn).to_be_disabled()

    # Typing a model name unblocks Create.
    modal.get_by_placeholder("Model name").fill("text-embedding-test")
    expect(create_btn).to_be_enabled()
