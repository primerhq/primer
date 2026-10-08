"""System > Activity: the OCCURRED column shows a short local time and keeps the stored value as a tooltip (ADM-32 of the 2026-10-08 admin review).

Every row used to print the storage format, ``2026-10-07T20:13:43.972680Z``. The page is driven for real against whatever events the instance holds (a bootstrapped instance always has
some: the seed writes collection and document events), so nothing is stubbed.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")

SHORT_TIME = re.compile(r"^(\d{1,2} [A-Z][a-z]{2} )?\d{2}:\d{2}:\d{2}$")
STORED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _open_activity(page: Page, console_url: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation), so wait for it and
    navigate once more if the Activity panel is not there after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("shell-activity")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", "system:activity")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def test_every_occurred_cell_is_a_short_time_with_the_stored_value_as_its_tooltip(page: Page, console_url: str) -> None:
    _open_activity(page, console_url)
    rows = page.locator("[data-testid^='activity-row:']")
    expect(rows.first).to_be_visible(timeout=20_000)

    cells = rows.locator("td:last-child")
    count = cells.count()
    assert count > 0
    for i in range(count):
        text = cells.nth(i).inner_text().strip()
        title = cells.nth(i).get_attribute("title") or ""
        assert SHORT_TIME.match(text), f"row {i} shows {text!r}, not a short time"
        assert STORED.match(title), f"row {i} has the tooltip {title!r}, not the stored value"

    # The first row names its date: a reader must not have to guess the day of a bare clock.
    assert re.match(r"^\d{1,2} [A-Z][a-z]{2} \d{2}:\d{2}:\d{2}$", cells.first.inner_text().strip())
