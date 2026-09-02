"""The catalog is one reachable surface with every family on its chips."""

from __future__ import annotations

from tests.ui_e2e._studio_helpers import open_provider_catalog


# The keys the catalog actually uses, which are also the ones the URL
# carries (?overlay=providers:<key>[:<id>]) and the ones
# LEGACY_ROUTE_OVERLAYS maps the old routes onto. Four here were the
# plural or the short form instead -- vector, artifact, workspaces,
# channels -- and named no class on the rail.
CLASS_KEYS = [
    "llm",
    "embedding",
    "cross_encoder",
    "ssp",
    "stt",
    "tts",
    "web_search",
    "web_fetch",
    "artifact_storage",
    "workspace",
    "channel",
]


def test_every_family_chip_opens_its_body(page, console_url) -> None:
    # RETARGET (platform wave P1a item 1): the rail became a family
    # chips row - provider-class-{key} is now provider-chip-{key}.
    open_provider_catalog(page, console_url)
    for key in CLASS_KEYS:
        page.click(f'[data-testid="provider-chip-{key}"]')
        page.wait_for_selector(f'[data-testid="provider-body-{key}"]')


def test_the_speech_classes_expose_the_active_defaults_panel(page, console_url) -> None:
    open_provider_catalog(page, console_url, cls="tts")
    page.wait_for_selector('[data-testid="active-speech-config"]')
    page.wait_for_selector('[data-testid="active-speech-voice"]')


def test_the_llm_class_offers_the_shared_form(page, console_url) -> None:
    # RETARGET (01a063ab, designer reconciliation): Register now lists
    # the active class's KINDS directly (PC_RegisterDropdown), each
    # annotated from the served _types data - picking "anthropic" opens
    # the form with that kind already preselected, no in-form re-pick
    # left. Anthropic is a discoverable kind (providers.py), so its form
    # shows the Live-model-probe panel's own Test connect button
    # (provider-probe-test) - the generic footer Test button
    # (provider-form-test) only survives for classes with no probe panel.
    open_provider_catalog(page, console_url, cls="llm")
    page.click('[data-testid="provider-register-toggle"]')
    page.click('[data-testid="provider-register-kind-anthropic"]')
    form = page.get_by_test_id("provider-form-llm_providers")
    form.wait_for(state="visible", timeout=15_000)
    page.wait_for_selector('[data-testid="provider-probe-test"]')


def test_the_catalog_is_reachable_from_the_sidebar(page, console_url) -> None:
    open_provider_catalog(page, console_url, via="sidebar")


def test_a_provider_row_can_be_created_and_deleted(page, console_url) -> None:
    # RETARGET (01a063ab, designer reconciliation): Register now lists
    # the active class's KINDS directly (PC_RegisterDropdown) - picking
    # "openai" opens the form with that kind already preselected, no
    # in-form re-pick left. The card grid's own per-row footer button
    # still replaces the old select-row-then-use-the-side-panel delete.
    open_provider_catalog(page, console_url, cls="stt")
    page.click('[data-testid="provider-register-toggle"]')
    page.click('[data-testid="provider-register-kind-openai"]')
    form_locator = page.get_by_test_id("provider-form-stt_providers")
    form_locator.wait_for(state="visible", timeout=15_000)
    form = '[data-testid="provider-form-stt_providers"]'
    page.fill(f'{form} [data-field="id"] input', "journey-stt")
    # default_model is required on SpeechToTextProvider, so Save stays
    # disabled until it is filled: the form declares that now instead of
    # letting the create come back 422 with nothing saying which field
    # was missing.
    page.fill(f'{form} [data-field="default_model"] input', "whisper-1")
    # url is required on the speech config too.
    page.fill(f'{form} [data-field="url"] input', "https://api.openai.com/v1")
    page.click('[data-testid="provider-form-save"]')
    page.wait_for_selector("text=journey-stt")

    # Coverage (01a063ab): Open is now a text link on the card footer,
    # not a button - reopening the SAME form/edit overlay confirms it
    # still hands back the full row (editing=true), then Cancel returns
    # to the grid without mutating anything.
    page.click('[data-testid="provider-card-open-journey-stt"]')
    form_locator.wait_for(state="visible", timeout=15_000)
    page.click('[data-testid="provider-form-cancel"]')
    form_locator.wait_for(state="hidden", timeout=15_000)

    # Coverage (01a063ab): the header Filter input narrows the visible
    # cards by name/kind, client-side, without touching the "N entities"
    # count (that count reflects the class's real total, not the
    # filtered subset - PC_InstanceGrid's own documented behavior).
    count_before = page.get_by_test_id("provider-entity-count").inner_text()
    page.fill('[data-testid="provider-filter"]', "no-such-provider-xyz")
    page.wait_for_selector('[data-testid="provider-filter-empty-stt"]')
    assert page.get_by_test_id("provider-card-journey-stt").count() == 0
    assert page.get_by_test_id("provider-entity-count").inner_text() == count_before
    page.fill('[data-testid="provider-filter"]', "journey-stt")
    page.wait_for_selector('[data-testid="provider-card-journey-stt"]')
    page.fill('[data-testid="provider-filter"]', "")

    page.click('[data-testid="provider-card-delete-journey-stt"]')
    page.click('[data-testid="provider-card-delete-confirm-journey-stt"]')
