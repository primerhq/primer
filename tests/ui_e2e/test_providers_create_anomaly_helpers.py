"""Anomaly-helper-text regression tests for the provider create modals.

Each documented anomaly that has a UI surface in the LLM provider create
form gets a tiny assert-only test here. No backend precondition needed
— the helper text is in the JSX and renders whenever the modal opens.

Currently covers:
* U0010 — T0025 static-models helper under the models table. Asserted on
  the EMBEDDING modal: the LLM form has no models table any more, because
  an LLM provider's registry is its ModelProfile rows.

Future tests (T0379, etc.) belong in this same file under the same
shape so the loop's generator + picker treat them as a cohesive batch.
"""

from __future__ import annotations


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route
pytestmark = smk("SMK-UI-02")


def test_u0010_embedding_provider_modal_shows_t0025_static_models_helper(
    page,
    console_url: str,
) -> None:
    """U0010 — Opening the New embedding provider modal renders the
    documented T0025 helper text ("Model list comes from the provider row,
    not a live introspection (T0025)") under the models table.

    Asserted on the EMBEDDING form rather than the LLM one: only the
    embedding and cross-encoder families still carry a models[], so they
    are the only ones the static-list anomaly still describes. The LLM
    form's models table is gone -- its ModelProfile rows are the registry.

    The text is unconditional UI copy in PROVIDER_FIELDS' Models section
    — no backend precondition required. We assert both the human-readable
    phrasing and the (T0025) tag are present so a future copy-edit can't
    silently drop the anomaly reference.
    """
    # The catalog creates through an inline form, not a modal: the
    # per-class New-provider modals are what it replaced.
    open_legacy_route(page, console_url, "providers/embedding")
    form = page.get_by_test_id("provider-form-embedding_providers")
    form.wait_for(state="visible", timeout=15_000)

    # The helper is field help on the models list, which the API supplies
    # with the rest of the form shape. Substring matches so a punctuation
    # tweak does not break this, but the key phrase is pinned.
    modal_text = form.inner_text()
    assert "Model list comes from the provider row" in modal_text, (
        "Expected the T0025 helper on the embedding provider form; the "
        "form did not contain the documented phrasing.\n"
        f"Form text was:\n{modal_text}"
    )
    assert "T0025" in modal_text, (
        "T0025 anomaly tag missing from the form. Documented anomalies "
        "are surfaced in place, not hidden (docs/dev/subsystems/"
        "ui-pages.md).\nForm text was:\n" + modal_text
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
    open_legacy_route(page, console_url, "providers/llm")
    form = page.get_by_test_id("provider-form-llm_providers")
    form.wait_for(state="visible", timeout=15_000)

    modal_text = form.inner_text()
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

    Re-pointed at the catalog's inline form: the per-class create modal
    this was written against is gone, and the form that replaced it
    shipped with no gate at all, so the same empty row went out as
    ``models: [{}]`` again.
    """
    from playwright.sync_api import expect

    open_legacy_route(page, console_url, "providers/embedding")
    form = page.get_by_test_id("provider-form-embedding_providers")
    form.wait_for(state="visible", timeout=15_000)

    save_btn = form.get_by_test_id("provider-form-save")
    model_list = form.get_by_test_id("provider-form-model-list")

    # The subject here is the MODEL-ROW gate, so satisfy the row-level
    # one first: an id is required on every provider, and Save is now
    # withheld until every required field is filled rather than letting
    # the create come back 422.
    form.locator('[data-field="id"] input').fill("emb-gate-probe")
    # Base URL is required on this class too. Filling every OTHER
    # required field is what isolates the model-row gate: whatever
    # Save's state is after this line, only the blank model row explains
    # it.
    form.locator('[data-field="url"] input').fill("https://api.openai.com/v1")

    # Add a model row but leave its name blank: the regression case.
    form.get_by_test_id("provider-form-add-model").click()
    row_input = model_list.locator("input.mono").last
    expect(row_input).to_be_visible()
    expect(save_btn).to_be_disabled()

    # Typing a model name unblocks Save.
    row_input.fill("text-embedding-test")
    expect(save_btn).to_be_enabled()
