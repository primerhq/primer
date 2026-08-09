"""Studio2 trial shell journey: mount, palette, tabs, legacy frame.

Runs against the live console (scripts/e2e/ui-bringup.sh or an
equivalent server with auth disabled), gated by PRIMER_RUN_UI_E2E=1
like every module in this suite.
"""

from __future__ import annotations


def test_studio2_shell_journey(page, console_url, console_messages) -> None:
    page.goto(console_url + "#/studio2")
    page.wait_for_selector('[data-testid="s2-root"]')

    # Seven regions render.
    for tid in ["s2-menubar", "s2-rail", "s2-nav", "s2-center",
                "s2-right", "s2-status", "s2-term"]:
        assert page.locator(f'[data-testid="{tid}"]').count() == 1, tid

    # Eleven rail groups.
    assert page.locator('[data-testid="s2-rail-list"] [role="tab"]').count() == 11

    # Palette opens and runs a command (navigator switch to Agents).
    page.keyboard.press("Control+k")
    page.wait_for_selector('[data-testid="s2-palette-input"]')
    page.fill('[data-testid="s2-palette-input"]', "Agents navigator")
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="s2-nav-filter"]')

    # Quick-open reaches a classic page; it renders in an iframe tab.
    page.keyboard.press("Control+p")
    page.wait_for_selector('[data-testid="s2-palette-input"]')
    page.fill('[data-testid="s2-palette-input"]', "LLM Providers")
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="s2-legacy-frame"]')
    frame = page.frame_locator('[data-testid="s2-legacy-frame"]')
    frame.get_by_role("heading", name="LLM providers").wait_for(timeout=15000)

    # The tab bar shows the doc; Ctrl+W closes it back to the welcome
    # screen (no crash, tab gone).
    assert page.locator('[data-testid="s2-tabbar"] [role="tab"]').count() >= 1
    page.keyboard.press("Control+w")
    page.wait_for_timeout(200)
    assert page.locator('[data-testid="s2-legacy-frame"]').count() == 0

    # The deep link restores a document on a real page load (the ?open=
    # param is parsed at shell mount; a same-document hash change is not
    # a load, so reload explicitly like a pasted link would).
    page.goto(console_url + "#/studio2?open=legacy:%2Fhealth")
    page.reload()
    page.wait_for_selector('[data-testid="s2-legacy-frame"]')

    # Exit trial returns to the classic console chrome.
    page.click("text=exit trial")
    page.wait_for_selector("text=Dashboard")

    # No uncaught page errors and no CSP violations. (Benign 404s from
    # optional endpoints on a fresh database are tolerated; a strict
    # zero-console-error bar belongs to the bringup environment.)
    hard = [m for m in console_messages
            if m["level"] == "pageerror" or "violates" in (m["text"] or "")]
    assert not hard, hard
