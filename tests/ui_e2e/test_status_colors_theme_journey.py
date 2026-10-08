"""Journey: the status text colours (parked chip, failed turn, failed tool call) are legible in the light theme too (console review C-030).

The stylesheet used ``var(--warn, #d9a441)`` and ``var(--danger, #e06c5f)`` with neither token declared, so both themes got the dark theme's
literal: 2.1:1 and 3.0:1 on the light surfaces. The tokens are now declared per theme (``tests/ui/test_token_contrast.py`` holds the declared
values to AA); this measures what the browser actually PAINTS, which is what the review measured: each class is put on the page under each
theme, its computed colour and the page background are both resolved to sRGB through a canvas (the browser hands back ``oklch(...)``), and the
WCAG ratio is computed from those. Nothing is mocked and no session is needed: the rules are plain class selectors.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_gate

pytestmark = smk("SMK-UI-06", status="partial")

_MEASURE = """
(theme) => {
  document.documentElement.setAttribute("data-theme", theme);
  const probe = document.createElement("div");
  probe.setAttribute("data-testid", "status-color-probe");
  probe.innerHTML =
    '<span class="nv-session-state-chip" data-state="parked">Parked</span>' +
    '<div class="nv-turn-error">failed</div>' +
    '<span class="nv-toolblock-err">err</span>' +
    '<div class="nv-attach-chip" data-status="error">file</div>';
  document.body.appendChild(probe);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const srgb = (css) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = "#000";
    ctx.fillStyle = css;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  };
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const bg = srgb(getComputedStyle(document.body).backgroundColor);
  const out = {};
  for (const [name, sel] of [
    ["parked chip", ".nv-session-state-chip"], ["failed turn", ".nv-turn-error"],
    ["failed tool call", ".nv-toolblock-err"], ["failed attachment", ".nv-attach-chip"],
  ]) {
    const color = getComputedStyle(probe.querySelector(sel)).color;
    out[name] = { color, ratio: ratio(srgb(color), bg) };
  }
  probe.remove();
  return out;
}
"""


@pytest.mark.ui_e2e
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_each_status_colour_clears_aa_on_the_page_background_in_both_themes(page: Page, console_url: str, theme: str) -> None:
    open_gate(page, console_url)
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=30_000)

    measured = page.evaluate(_MEASURE, theme)

    assert set(measured) == {"parked chip", "failed turn", "failed tool call", "failed attachment"}
    low = {name: (m["color"], round(m["ratio"], 2)) for name, m in measured.items() if m["ratio"] < 4.5}
    assert not low, f"{theme} theme, below AA (4.5:1) on the page background: {low}"
