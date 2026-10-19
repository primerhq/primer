"""The Platform page conventions on a real page (Platform revamp Plan 01, Task 10; design 5.1 to 5.3, 13.5 and 20 Q1).

Every generic Platform page draws through the platform kit: the title on the left; on the right the filter, the pager and the page's one compact action, in that order and at one height;
a full-width grid of twelve cards a page; and Delete in the card's "⋯" menu, with the prompt it always had. These journeys drive the Agents page with thirteen seeded agents (one more
than a page holds), which every test narrows to its own by the unique suffix all thirteen ids carry:

* the filter, the pager and the action sit on one row, within 2px of one height, in that order; twelve cards show, the range reads ``1-12 of 13``, the thirteenth is on the next page, a
  filter typed there returns to the first, and there is one pager only (none below the grid);
* the grid fills the pane: its right edge is within 32px of the pane's (the 1020px cap left more than 200px at this window width);
* a card has no Delete of its own; the "⋯" menu has it, and it asks "Permanently delete <id>? Referenced entities refuse deletion." and deletes nothing when cancelled;
* Escape with the menu open closes only the menu (the page and the address stay), a click outside closes it too, and neither opens the card under the menu;
* the menu opened from the keyboard puts the focus in the list, and Tab leaves it: the list closes and the focus moves on, to the card's Open.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_view

pytestmark = smk("SMK-UI-03", status="partial")

SEEDED = 13                                  # one more than a page holds
WINDOW = {"width": 1600, "height": 900}      # wide enough that the old 1020px cap leaves a visible gap


@pytest.fixture
def thirteen_agents(base_url: str, unique_suffix: str):
    """Thirteen agents whose ids all contain ``unique_suffix`` (and so match a filter on it), and the LLM provider they use; all removed afterwards."""
    provider_id = f"llm-conv-{unique_suffix}"
    ids = [f"conv-{unique_suffix}-{i:02d}" for i in range(SEEDED)]
    with httpx.Client(base_url=base_url, timeout=60.0) as c:
        r = seed_llm_provider_with(c, {
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"
        for agent_id in ids:
            r = c.post("/v1/agents", json={
                "id": agent_id,
                "description": "page conventions journey",
                "model": agent_model(provider_id, "fake-model"),
                "tools": [],
                "system_prompt": ["test"],
            })
            assert r.status_code == 201, f"seed agent {agent_id} failed: {r.text}"
    try:
        yield unique_suffix, ids
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for path in [f"/v1/agents/{agent_id}" for agent_id in ids] + [f"/v1/llm_providers/{provider_id}"]:
                try:
                    c.delete(path)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass


def _open_agents_filtered(page: Page, console_url: str, query: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation to the Studio view), so wait for it and
    navigate once more if the page is not the Platform one after a short wait; then type the filter, which narrows the list to this journey's agents."""
    page.set_viewport_size(WINDOW)
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    box = page.get_by_test_id("nv-plat-filter")
    for attempt in (1, 2):
        open_view(page, console_url, default_wid(console_url), "platform:agents")
        try:
            expect(box).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            break
        except AssertionError:
            if attempt == 2:
                raise
    box.fill(query)


def test_the_filter_the_pager_and_the_action_sit_on_one_row_at_one_height(page: Page, console_url: str, thirteen_agents) -> None:
    suffix, _ids = thirteen_agents
    _open_agents_filtered(page, console_url, suffix)
    expect(page.locator("[data-testid^='pk-card:']")).to_have_count(12, timeout=15_000)

    boxes = {
        "filter": page.locator(".pk-filter").bounding_box(),
        "pager": page.get_by_test_id("pk-pager").bounding_box(),
        "action": page.get_by_test_id("nv-plat-create").bounding_box(),
    }
    assert all(boxes.values()), boxes
    centres = [b["y"] + b["height"] / 2 for b in boxes.values()]
    assert max(centres) - min(centres) <= 2, f"the three controls are not on one row: {boxes}"
    heights = [b["height"] for b in boxes.values()]
    assert max(heights) - min(heights) <= 2, f"the three controls are not one height: {boxes}"
    assert boxes["filter"]["x"] < boxes["pager"]["x"] < boxes["action"]["x"], f"the order is filter, pager, action: {boxes}"
    expect(page.get_by_test_id("nv-plat-create")).to_have_text("+ Agent")


def test_twelve_cards_a_page_with_the_pager_beside_the_filter_and_none_below(page: Page, console_url: str, thirteen_agents) -> None:
    suffix, _ids = thirteen_agents
    _open_agents_filtered(page, console_url, suffix)
    cards = page.locator("[data-testid^='pk-card:']")
    expect(cards).to_have_count(12, timeout=15_000)
    expect(page.get_by_test_id("pk-pager-range")).to_have_text("1-12 of 13")
    expect(page.get_by_test_id("pk-count")).to_have_text(re.compile(r"^13 (of \d+ )?agents$"))      # "13 of N agents" while the filter hides the others
    expect(page.locator("[data-testid='pk-pager']")).to_have_count(1)
    expect(page.locator(".nv-pager")).to_have_count(0)

    page.get_by_test_id("pk-pager-next").click()
    expect(cards).to_have_count(1)
    expect(page.get_by_test_id("pk-pager-range")).to_have_text("13-13 of 13")
    expect(page.get_by_test_id("pk-pager-next")).to_be_disabled()

    # a filter typed on page two returns to page one
    page.get_by_test_id("nv-plat-filter").fill(f"{suffix}-0")
    expect(page.get_by_test_id("pk-pager-range")).to_have_text("1-10 of 10")
    expect(cards).to_have_count(10)


def test_the_grid_fills_the_pane(page: Page, console_url: str, thirteen_agents) -> None:
    suffix, _ids = thirteen_agents
    _open_agents_filtered(page, console_url, suffix)
    expect(page.locator("[data-testid^='pk-card:']")).to_have_count(12, timeout=15_000)
    # the pane's client area ends where its scrollbar starts, so the scrollbar (if one is drawn) is not counted as a gap
    gap = page.evaluate(
        """() => {
            const pane = document.querySelector('.nv-plat-main');
            const grid = document.querySelector('[data-testid="nv-plat-grid"]');
            return pane.getBoundingClientRect().left + pane.clientWidth - grid.getBoundingClientRect().right;
        }"""
    )
    assert 0 <= gap <= 32, f"the grid stops {gap}px short of the right edge of the pane (the padding is 24px; a width cap leaves far more)"


def test_delete_is_in_the_cards_menu_and_asks_what_it_always_asked(page: Page, base_url: str, console_url: str, thirteen_agents) -> None:
    _suffix, ids = thirteen_agents
    target = ids[0]
    _open_agents_filtered(page, console_url, target)
    card = page.get_by_test_id(f"pk-card:{target}")
    expect(card).to_be_visible(timeout=15_000)
    expect(card.get_by_role("button", name="Delete")).to_have_count(0)

    page.get_by_test_id(f"pk-card-menu:{target}").click()
    delete = page.get_by_test_id(f"nv-plat-delete:{target}")
    expect(delete).to_be_visible()
    delete.click()

    dialog = page.locator(".modal-overlay")
    expect(dialog).to_contain_text(f"Permanently delete {target}?", timeout=5_000)
    expect(dialog).to_contain_text("Referenced entities refuse deletion")
    dialog.get_by_role("button", name="Cancel").click()
    expect(page.locator(".modal-overlay")).to_have_count(0)
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        assert c.get(f"/v1/agents/{target}").status_code == 200, "cancelling the prompt deleted the agent"


def test_escape_closes_only_the_menu_and_a_click_outside_closes_it_too(page: Page, console_url: str, thirteen_agents) -> None:
    _suffix, ids = thirteen_agents
    target = ids[1]
    _open_agents_filtered(page, console_url, target)
    button = page.get_by_test_id(f"pk-card-menu:{target}")
    menu = page.get_by_test_id(f"pk-card-menu-list:{target}")
    expect(button).to_be_visible(timeout=15_000)
    before = page.url

    button.click()
    expect(menu).to_be_visible()
    expect(button).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)      # the "⋯" never opens the card under it
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(button).to_have_attribute("aria-expanded", "false")
    expect(button).to_be_focused()
    expect(page.get_by_test_id("nv-plat-page:agents")).to_be_visible()
    assert page.url == before, "Escape with the menu open changed the address"

    button.click()
    expect(menu).to_be_visible()
    page.locator(".pk-title").click()
    expect(menu).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert page.url == before, "a click outside the menu changed the address"


def test_the_menu_opened_from_the_keyboard_is_left_with_tab(page: Page, console_url: str, thirteen_agents) -> None:
    _suffix, ids = thirteen_agents
    target = ids[2]
    _open_agents_filtered(page, console_url, target)
    button = page.get_by_test_id(f"pk-card-menu:{target}")
    menu = page.get_by_test_id(f"pk-card-menu-list:{target}")
    expect(button).to_be_visible(timeout=15_000)

    button.focus()
    page.keyboard.press("Enter")
    expect(menu).to_be_visible()
    expect(page.get_by_test_id(f"nv-plat-delete:{target}")).to_be_focused()        # the list takes the focus, on its first item
    page.keyboard.press("Tab")
    expect(menu).to_have_count(0)                                                   # Tab leaves the list, which closes
    expect(page.get_by_test_id(f"pk-card-open:{target}")).to_be_focused()           # and the focus moves on to the card's Open
