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
    # RETARGET (01a063ab, designer reconciliation): Register now lists
    # the active class's KINDS directly (PC_RegisterDropdown) - kind
    # arrives preselected, there is no more in-form kind picker to
    # re-pick it from.
    open_legacy_route(page, console_url, "providers/embedding")
    page.get_by_test_id("provider-register-toggle").click()
    page.get_by_test_id("provider-register-kind-openai").click()
    form = page.get_by_test_id("provider-form-embedding_providers")
    form.wait_for(state="visible", timeout=15_000)
    # The form root renders before /\_types resolves, so wait for the
    # field the helper hangs off before reading the text: a bare
    # inner_text() here reads an empty shell and reports the copy as
    # missing.
    form.locator('[data-field="models"]').wait_for(
        state="visible", timeout=15_000,
    )

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


## test_u0011_llm_provider_modal_shows_t0379_cross_validation_warning
## retired (01a063ab, designer reconciliation): the T0379 warning existed
## to flag a mismatch between a manually re-picked kind and the fields
## shown for it. That mismatch class is now structurally impossible -
## kind always arrives preselected from PC_RegisterDropdown (create) or
## is the row's own real value (edit), with no in-form control left to
## re-pick it from - so provider-form.jsx dropped both the kind <select>
## and the warning it explained (see the "Designer reconciliation"
## comment above PC_ProviderForm's `fields` block). Flagged in the PR
## body per the task's own instruction; not replaced, since there is no
## remaining surface for this anomaly to occupy.


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

    RETARGET (01a063ab, designer reconciliation): Register now lists the
    active class's KINDS directly (PC_RegisterDropdown) - kind arrives
    preselected, so this opens straight on the openai embedding form
    (same testids, same gate logic once it renders).
    """
    from playwright.sync_api import expect

    open_legacy_route(page, console_url, "providers/embedding")
    page.get_by_test_id("provider-register-toggle").click()
    page.get_by_test_id("provider-register-kind-openai").click()
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
