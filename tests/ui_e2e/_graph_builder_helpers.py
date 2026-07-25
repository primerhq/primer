"""Shared gestures for driving the revamped graph builder (GB_Builder).

The builder replaced the previous editor's toolbar ("Add node" -> a kind
dropdown, a side panel keyed on node id) with a purpose-first palette and an
inspector keyed on the human label, so the journeys that pinned the old
gestures drive these helpers instead. The *contracts* they pin are unchanged:
Save reflects a real diff, Discard reverts, layout-only changes never dirty.

Testids come from ui/graph-builder/WIRING.md §14.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# The builder is behind the `graphBuilderV2` tweak (default on).
BUILDER = '[data-testid="gb-builder"]'
SAVE = '[data-testid="gb-save"]'
DISCARD = '[data-testid="gb-discard"]'
TIDY = '[data-testid="gb-tidy"]'
CANVAS = '[data-testid="graph-canvas"]'
OUTLINE = '[data-testid="gb-outline"]'
OUTLINE_ADD = '[data-testid="gb-outline-add"]'
PALETTE = '[data-testid="gb-palette"]'
INSPECTOR = '[data-testid="gb-inspector"]'
DIRTY = '[data-testid="gb-dirty"]'


def wait_for_builder(page: Page, timeout: int = 20_000):
    """Wait for the builder shell to mount and return its locator."""
    builder = page.locator(BUILDER)
    builder.wait_for(state="visible", timeout=timeout)
    return builder


def save_button(page: Page):
    """The Save control ('Save draft'), addressed by testid not label."""
    return page.locator(SAVE).first


def discard_button(page: Page):
    return page.locator(DISCARD).first


def add_finish_step(page: Page, timeout: int = 10_000) -> None:
    """Add a Finish (End) step through the purpose-first palette.

    The closest equivalent of the old "Add node -> Terminal" gesture: the
    `end` purpose is the one that needs no follow-up reference, so it creates
    the node immediately and stages a real structural change.
    """
    page.locator(OUTLINE_ADD).first.click()
    palette = page.locator(PALETTE)
    palette.wait_for(state="visible", timeout=timeout)
    palette.locator('[data-testid="gb-palette-row"][data-purpose="end"]').first.click()
    palette.wait_for(state="hidden", timeout=timeout)


def expect_clean(page: Page, timeout: int = 10_000) -> None:
    """No staged edits: Save disabled and the dirty dot absent."""
    expect(save_button(page)).to_be_disabled(timeout=timeout)
    expect(page.locator(DIRTY)).to_have_count(0, timeout=timeout)


def expect_dirty(page: Page, timeout: int = 10_000) -> None:
    """Staged edits present: Save enabled and the dirty dot shown."""
    expect(save_button(page)).to_be_enabled(timeout=timeout)
    expect(page.locator(DIRTY).first).to_be_visible(timeout=timeout)


def outline_row_count(page: Page) -> int:
    return page.locator('[data-testid="gb-outline-row"]').count()
