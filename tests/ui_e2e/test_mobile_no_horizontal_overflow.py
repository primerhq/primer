"""No console page may scroll the DOCUMENT sideways on a phone.

Regression: the topbar and `.main` are grid items, so both defaulted to
`min-width: auto`, which floors an item at its min-content width. The
topbar's min-content is ~410px (brand + hamburger + the status cluster,
whose worker pill alone was 182px and could not shrink), so on any
viewport narrower than that the row forced the whole document wider than
the screen. Every page was affected, not one -- it is shell chrome.

The distinction this pins: chrome must fit the viewport. Content that
genuinely needs more width is allowed to scroll, but inside `.main`
(which already has `overflow: auto`), never by dragging the document.

Measured rather than eyeballed: `documentElement.scrollWidth` against
`clientWidth` is the whole assertion, and it is the one thing a
presence-based test can never notice.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page  # noqa: E402


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-01", status="partial")


# Two real device widths. 360 is the narrowest mainstream Android; 390 is the
# modern iPhone. Both sat under the old 410px floor.
PHONE_WIDTHS = [(390, 844), (360, 800)]

# One per top-level nav section that renders the standard shell.
# "" means the shell root itself; the rest are legacy routes the
# facade translates into overlays. The chats route went with the chat
# surface in S6.
SURFACES = ["", "agents", "workspaces", "toolsets"]


def _overflow(page: Page) -> dict:
    return page.evaluate(
        """
        () => {
          const de = document.documentElement;
          const widest = [];
          document.querySelectorAll('*').forEach(e => {
            const r = e.getBoundingClientRect();
            if (r.width > de.clientWidth + 2) {
              widest.push({
                cls: (e.className || '').toString().slice(0, 40),
                w: Math.round(r.width),
              });
            }
          });
          widest.sort((a, b) => b.w - a.w);
          return {
            clientWidth: de.clientWidth,
            scrollWidth: de.scrollWidth,
            // The shell itself must never be a culprit.
            topbar: (() => {
              const e = document.querySelector('.nv-topbar');
              return e ? Math.round(e.getBoundingClientRect().width) : null;
            })(),
            widest: widest.slice(0, 3),
          };
        }
        """
    )


@pytest.mark.ui_e2e
@pytest.mark.parametrize(("width", "height"), PHONE_WIDTHS)
@pytest.mark.parametrize("route", SURFACES)
def test_the_document_does_not_scroll_sideways_on_a_phone(
    page: Page, console_url: str, route: str, width: int, height: int,
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    if route:
        open_legacy_route(page, console_url, route)
    else:
        page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    # The shell mounts before data arrives; give the topbar's live pill a beat
    # so we measure it populated rather than empty.
    page.wait_for_timeout(1500)

    m = _overflow(page)
    assert m["scrollWidth"] <= m["clientWidth"] + 1, (
        f"{route} at {width}px scrolls sideways "
        f"({m['scrollWidth']} > {m['clientWidth']}): {m['widest']}"
    )


@pytest.mark.ui_e2e
@pytest.mark.parametrize(("width", "height"), PHONE_WIDTHS)
def test_the_topbar_fits_the_viewport(
    page: Page, console_url: str, width: int, height: int,
) -> None:
    """The specific element that was floored. Asserted separately so a
    regression here is not masked by a page whose content also overflows."""
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)

    m = _overflow(page)
    assert m["topbar"] is not None, "the app topbar never mounted"
    assert m["topbar"] <= m["clientWidth"] + 1, (
        f"topbar is {m['topbar']}px in a {m['clientWidth']}px viewport"
    )


@pytest.mark.ui_e2e
def test_the_worker_pill_stays_one_line_on_a_phone(page: Page, console_url: str) -> None:
    """Letting the pill shrink is only half the fix -- unconstrained it wraps
    'N/N workers - N in flight' mid-phrase and doubles its own height."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{console_url}#/")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)

    pill = page.locator(".worker-pill")
    if pill.count() == 0:  # pragma: no cover - health probe may be absent
        pytest.skip("no worker pill rendered on this deployment")

    box = pill.first.bounding_box()
    assert box is not None
    # One line of 11.5px monospace plus padding lands near 27px; wrapped it was
    # 43px. 34 splits them without pinning the exact metrics.
    assert box["height"] < 34, (
        f"worker pill is {box['height']}px tall - it is wrapping"
    )
