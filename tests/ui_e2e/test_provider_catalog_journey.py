"""The catalog is one reachable surface with every family on its cards."""

from __future__ import annotations

from tests.ui_e2e._studio_helpers import open_provider_catalog


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


def test_every_type_page_shows_only_its_own_cards(page, console_url) -> None:
    for key in CLASS_KEYS:
        open_provider_catalog(page, console_url, cls=key)
        page.wait_for_selector(f'[data-testid="provider-body-{key}"]')
        cards = page.locator(f'[data-testid="provider-body-{key}"] [data-testid^="pk-card:"]')
        count = cards.count()
        for i in range(count):
            card_tid = cards.nth(i).get_attribute("data-testid")
            assert card_tid.startswith(f"pk-card:{key}:")


def test_the_speech_classes_expose_the_active_defaults_panel(page, console_url) -> None:
    open_provider_catalog(page, console_url, cls="tts")
    page.wait_for_selector('[data-testid="pc-active-tts"]')
    open_provider_catalog(page, console_url, cls="stt")
    page.wait_for_selector('[data-testid="pc-active-stt"]')


def test_register_opens_the_overlay_on_the_type(page, console_url) -> None:
    open_provider_catalog(page, console_url, cls="llm")
    page.click('[data-testid="pc-register-open"]')
    page.wait_for_selector('[data-testid="pc-register"]')
    page.wait_for_selector('[data-testid="pc-field:url"]')
    page.wait_for_selector('[data-testid="pc-limit:max_retries"]')


def test_the_catalog_is_reachable_from_the_sidebar(page, console_url) -> None:
    open_provider_catalog(page, console_url, via="sidebar")


def test_a_provider_row_can_be_created_and_deleted(page, console_url, mock_llm_lan) -> None:
    _registry, mock_base_url = mock_llm_lan
    open_provider_catalog(page, console_url, cls="stt")
    page.click('[data-testid="pc-register-open"]')
    page.wait_for_selector('[data-testid="pc-register"]')
    page.get_by_test_id("pc-field:id").fill("journey-stt")
    page.get_by_test_id("pc-field:url").fill(mock_base_url)
    if page.locator('[data-testid="pc-register-next"]').is_visible():
        page.click('[data-testid="pc-register-next"]')
        page.wait_for_selector('[data-testid="pc-speech-pick"]')
        page.locator('[data-testid^="pc-pick:"]').first.check()
        page.click('[data-testid="pc-register-submit"]')
    else:
        page.get_by_test_id("pc-field:default_model").fill("whisper-1")
        page.click('[data-testid="provider-form-save"]')
    page.wait_for_selector('[data-testid="pk-card:stt:journey-stt"]')

    page.fill('[data-testid="pc-filter"]', "no-such-provider-xyz")
    page.wait_for_selector('[data-testid="pc-filter-empty"]')
    assert page.locator('[data-testid="pk-card:stt:journey-stt"]').count() == 0
    page.fill('[data-testid="pc-filter"]', "journey-stt")
    page.wait_for_selector('[data-testid="pk-card:stt:journey-stt"]')
    page.fill('[data-testid="pc-filter"]', "")

    page.click('[data-testid="pk-card-menu:stt:journey-stt"]')
    page.click('[data-testid="pc-card-delete:stt:journey-stt"]')
    page.click('[data-testid="dialog-confirm"]')
    page.locator('[data-testid="pk-card:stt:journey-stt"]').wait_for(state="hidden", timeout=15_000)
