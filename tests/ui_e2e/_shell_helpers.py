"""Playwright helpers for the fresh shell (S8).

The facade amendment M16 asks for: every ui_e2e test written between S1
and S8 drives the console through a helper layer, so re-pointing at the
fresh shell is one edit here rather than N edits across the suite. P5
rewrites _studio_helpers.py to delegate to these functions.

Selectors mirror ui/components/shell/*.jsx exactly:

  shell-root / shell-topbar / shell-rail / shell-center / shell-statusbar
  shell-tab:<doc-id> · shell-overlay:<name> · shell-palette
  rail-sessions / rail-files / rail-attention · attention-item:<sid>
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

SHELL_CONSOLE_IGNORES = [
    r"net::ERR_ABORTED",
    r"favicon",
    r"status of 404",
]


def shell_url(console_url: str, wid: str) -> str:
    return f"{console_url}#/w/{wid}"


def open_shell(page: Page, console_url: str, wid: str, *, timeout: int = 20_000) -> None:
    page.goto(shell_url(console_url, wid))
    expect(page.get_by_test_id("shell-root")).to_be_visible(timeout=timeout)


def open_doc(page: Page, console_url: str, wid: str, kind: str, ref: str,
             *, anchor: str | None = None, timeout: int = 20_000) -> None:
    url = f"{console_url}#/w/{wid}?doc={kind}:{ref}"
    if anchor:
        url += f"#{anchor}"
    page.goto(url)
    expect(page.get_by_test_id(f"shell-tab:{kind}:{ref}")).to_be_visible(timeout=timeout)


def open_overlay(page: Page, console_url: str, wid: str, name: str,
                 *, timeout: int = 20_000) -> None:
    page.goto(f"{console_url}#/w/{wid}?overlay={name}")
    expect(page.get_by_test_id(f"shell-overlay:{name}")).to_be_visible(timeout=timeout)


def open_palette(page: Page) -> None:
    page.keyboard.press("Control+k")
    expect(page.get_by_test_id("shell-palette")).to_be_visible()


def run_verb(page: Page, label: str) -> None:
    open_palette(page)
    page.get_by_test_id("shell-palette-input").fill(label)
    page.get_by_test_id("shell-palette-row").first.click()


def session_row(page: Page, sid: str):
    return page.get_by_test_id(f"rail-session:{sid}")


def attention_item(page: Page, sid: str):
    return page.get_by_test_id(f"attention-item:{sid}")
