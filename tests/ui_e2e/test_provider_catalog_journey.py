"""The catalog is one reachable surface with every class on its rail."""

from __future__ import annotations

from tests.ui_e2e._studio_helpers import open_provider_catalog


CLASS_KEYS = [
    "llm",
    "embedding",
    "cross_encoder",
    "vector",
    "stt",
    "tts",
    "web_search",
    "web_fetch",
    "artifact",
    "workspaces",
    "channels",
]


def test_every_class_on_the_rail_opens_its_body(page, console_url) -> None:
    open_provider_catalog(page, console_url)
    for key in CLASS_KEYS:
        page.click(f'[data-testid="provider-class-{key}"]')
        page.wait_for_selector(f'[data-testid="provider-body-{key}"]')


def test_the_speech_classes_expose_the_active_defaults_panel(page, console_url) -> None:
    open_provider_catalog(page, console_url, cls="tts")
    page.wait_for_selector('[data-testid="active-speech-config"]')
    page.wait_for_selector('[data-testid="active-speech-voice"]')


def test_the_llm_class_offers_the_shared_form(page, console_url) -> None:
    open_provider_catalog(page, console_url, cls="llm")
    page.wait_for_selector('[data-testid="provider-form-llm_providers"]')
    page.wait_for_selector('[data-testid="provider-form-test"]')
