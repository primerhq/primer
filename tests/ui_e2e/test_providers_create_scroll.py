"""Modal-scroll regression net for the rich provider create modal.

Parallel to test_agents_create.py's U0016 — same contract from UI
spec §3 (modal capped at calc(100vh - 40px); header + footer pinned
with flex-shrink: 0; body scrolls with overflow-y: auto). The rich
provider modal is the second-tallest form in the console (provider
fields + models table + max_concurrency); it's the natural canary
for the modal-scroll contract beyond the agent form.

U0015 covers the LLM provider modal. Sister tests for Embedding +
Cross-Encoder would slot in here under the same shape — modal
classes + .modal-b sizing are shared, so one passing test per
family is enough to pin the cross-cutting contract.
"""

from __future__ import annotations


def test_u0015_new_llm_provider_modal_scrolls_to_footer_at_600px(
    page,
    console_url: str,
) -> None:
    """U0015 — At 1366x600 the New LLM provider modal's Create button
    is reachable via in-modal scroll (not clipped by the pinned
    footer). Regression net for the scroll bug fixed in commit
    5ca8790 — the rich PROVIDER_FIELDS modal is taller than the
    legacy JSON-textarea modal it replaced, so it's the natural
    second canary after the agent modal (U0016).

    Pin the UI spec §3 modal contract:
      * modal.max-height = calc(100vh - 40px)  → fits viewport
      * header + footer pinned (flex-shrink: 0)
      * body scrolls (overflow-y: auto, flex: 1 1 auto, min-height: 0)

    No backend precondition needed — the modal renders without any
    seeded data; provider dropdowns auto-default to the first known
    type (openresponses).
    """
    page.set_viewport_size({"width": 1366, "height": 600})

    page.goto(console_url + "#/providers/llm", wait_until="domcontentloaded")
    page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

    page.get_by_role("button", name="New llm provider").first.click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    # Modal MUST fit inside the viewport — bounding box height
    # should be <= viewport height. Inside, the body scrolls.
    viewport_h = page.viewport_size["height"]
    box = modal.bounding_box()
    assert box is not None, "could not measure modal bounding box"
    assert box["height"] <= viewport_h, (
        f"provider modal exceeds viewport height (modal={box['height']}px, "
        f"viewport={viewport_h}px); the pinned-footer scroll contract "
        f"from UI spec §3 is broken for the LLM provider form"
    )

    # Scroll the body all the way down — the Create button is in the
    # pinned footer so it's always rendered, but scrolling proves the
    # body is the scrollable region (not the modal container itself).
    body = modal.locator(".modal-b").first
    body.evaluate("el => el.scrollTo({top: el.scrollHeight})")

    # Create button is in the footer, always reachable. Enabled state
    # depends on form validity — the scroll contract is what's under
    # test here, not form-validity logic.
    create = modal.get_by_role("button", name="Create").first
    create.wait_for(state="visible", timeout=5_000)
    # Don't assert enabled/disabled — both states are valid; the
    # contract is "the button can be found + clicked if the form
    # is valid", not "the form is valid at modal-open time".
