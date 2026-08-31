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
    # RETARGET (IA restructure 01a04d6a): "Register provider" now names
    # the TYPE up front (11 classes, independent of whichever chip is
    # active) rather than the kind - the kind select moved INSIDE the
    # form, unchanged in spirit from the P1a note this replaces ("names
    # the kind first" is no longer true at the top level, but the form
    # still opens with one, defaulted, and still lets it be re-picked).
    # Re-selecting anthropic explicitly keeps this test's actual
    # invariant (an LLM-class form renders, with the Test button) robust
    # to whatever kind happens to be _types' own default first entry.
    open_provider_catalog(page, console_url, cls="llm")
    page.click('[data-testid="provider-register-all-toggle"]')
    page.click('[data-testid="provider-register-type-llm"]')
    form = page.get_by_test_id("provider-form-llm_providers")
    form.wait_for(state="visible", timeout=15_000)
    form.locator("#pf-provider").select_option(value="anthropic")
    page.wait_for_selector('[data-testid="provider-form-test"]')


def test_the_catalog_is_reachable_from_the_sidebar(page, console_url) -> None:
    open_provider_catalog(page, console_url, via="sidebar")


def test_a_provider_row_can_be_created_and_deleted(page, console_url) -> None:
    # RETARGET (IA restructure 01a04d6a): Register provider now names the
    # TYPE ("stt"), not the kind directly - the kind ("openai") is
    # re-picked inside the form, same mechanic as every other retargeted
    # test in this pass. The card grid's own per-row footer button still
    # replaces the old select-row-then-use-the-side-panel delete.
    open_provider_catalog(page, console_url, cls="stt")
    page.click('[data-testid="provider-register-all-toggle"]')
    page.click('[data-testid="provider-register-type-stt"]')
    form_locator = page.get_by_test_id("provider-form-stt_providers")
    form_locator.wait_for(state="visible", timeout=15_000)
    form_locator.locator("#pf-provider").select_option(value="openai")
    page.wait_for_selector('[data-testid="provider-form-stt_providers"]')
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
    page.click('[data-testid="provider-card-delete-journey-stt"]')
    page.click('[data-testid="provider-card-delete-confirm-journey-stt"]')
