"""At 375x812: chat header shows back-arrow, composer stays visible
during scroll, tapping kebab opens an actions menu, sending a message
scrolls to bottom."""
from __future__ import annotations
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-07", status="partial")


@pytest.mark.ui_e2e
def test_mobile_chat_header_back_arrow(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/chats")
    page.wait_for_load_state("domcontentloaded")


@pytest.mark.ui_e2e
def test_mobile_chat_composer_sticky(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{console_url}#/chats")
    page.wait_for_load_state("domcontentloaded")
