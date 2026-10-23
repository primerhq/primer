"""Anomaly-helper-text regression tests for the provider create modals."""

from __future__ import annotations

from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = smk("SMK-UI-02")


def test_u0010_embedding_provider_modal_shows_t0025_static_models_helper(
    page,
    console_url: str,
) -> None:
    """U0010 — Opening the New embedding provider modal renders the
    documented T0025 helper text ("Model list comes from the provider row,
    not a live introspection (T0025)") under the models table.
    """
    open_provider_catalog(page, console_url, cls="embedding")
    page.get_by_test_id("pc-register-open").click()
    overlay = page.get_by_test_id("pc-register")
    overlay.wait_for(state="visible", timeout=15_000)
    overlay.locator('[data-testid="pc-field:type"]').select_option("huggingface")
    overlay.locator('[data-field="models"]').wait_for(
        state="visible", timeout=15_000,
    )

    modal_text = overlay.inner_text()
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


def test_provider_create_disabled_until_model_name_filled(
    page,
    console_url: str,
) -> None:
    """Regression: the New provider modal's Create button must stay
    disabled until every model row has its required fields filled.
    """
    open_provider_catalog(page, console_url, cls="embedding")
    page.get_by_test_id("pc-register-open").click()
    overlay = page.get_by_test_id("pc-register")
    overlay.wait_for(state="visible", timeout=15_000)
    overlay.locator('[data-testid="pc-field:type"]').select_option("huggingface")
    overlay.locator('[data-field="models"]').wait_for(
        state="visible", timeout=15_000,
    )

    save_btn = overlay.get_by_test_id("provider-form-save")
    model_list = overlay.get_by_test_id("provider-form-model-list")

    overlay.locator('[data-testid="pc-field:id"] input, [data-field="id"] input').first.fill("emb-gate-probe")

    overlay.get_by_test_id("provider-form-add-model").click()
    row_input = model_list.locator("input.mono").last
    expect(row_input).to_be_visible()
    expect(save_btn).to_be_disabled()

    row_input.fill("text-embedding-test")
    expect(save_btn).to_be_enabled()
