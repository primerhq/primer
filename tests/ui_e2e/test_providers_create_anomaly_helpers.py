"""Anomaly-helper-text regression tests for the provider create modals."""

from __future__ import annotations

from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = smk("SMK-UI-02")


def test_u0010_embedding_provider_modal_shows_huggingface_models_table(
    page,
    console_url: str,
) -> None:
    """U0010: Hugging Face renders the editable model table and + Add."""
    open_provider_catalog(page, console_url, cls="embedding")
    page.get_by_test_id("pc-register-open").click()
    overlay = page.get_by_test_id("pc-register")
    overlay.wait_for(state="visible", timeout=15_000)
    overlay.locator('[data-testid="pc-field:type"]').select_option("huggingface")
    models = overlay.get_by_test_id("pc-models")
    models.wait_for(
        state="visible", timeout=15_000,
    )
    expect(models.get_by_role("button", name="+ Add")).to_be_visible()
    expect(models.get_by_role("textbox", name="Model 1")).to_be_visible()


def test_provider_create_disabled_until_model_name_filled(
    page,
    console_url: str,
) -> None:
    """Next stays disabled until every model row has a name."""
    open_provider_catalog(page, console_url, cls="embedding")
    page.get_by_test_id("pc-register-open").click()
    overlay = page.get_by_test_id("pc-register")
    overlay.wait_for(state="visible", timeout=15_000)
    overlay.locator('[data-testid="pc-field:type"]').select_option("huggingface")
    models = overlay.get_by_test_id("pc-models")
    models.wait_for(
        state="visible", timeout=15_000,
    )

    overlay.get_by_test_id("pc-field:id").fill("emb-gate-probe")
    next_btn = overlay.get_by_test_id("pc-register-next")
    row_input = models.get_by_role("textbox", name="Model 1")
    expect(next_btn).to_be_disabled()

    row_input.fill("text-embedding-test")
    expect(next_btn).to_be_enabled()
