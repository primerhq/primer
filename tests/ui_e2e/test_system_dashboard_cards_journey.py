"""Journey: the System dashboard's health cards say what the server says (console review C-036, the console part).

The scheduler card shows ``scheduler.detail`` from ``GET /v1/health`` under its word (the default install reports "in-memory scheduler (single process assumed)"; a scheduler with nothing
to add reads "healthy"), and the sessions-active card shows the ``total`` of ``GET /v1/sessions?session_state=running``: the turns in flight right now, not every session marked running.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests._support.yield_journeys import drive_park_on_tool
from tests.ui_e2e._delegation_seed import _run_to_completion
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-06", status="partial")


@pytest.mark.ui_e2e
def test_the_dashboard_cards_show_the_schedulers_detail_and_the_turns_in_flight(page: Page, base_url: str, console_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        scheduler = c.get("/v1/health").json()["scheduler"]
        running = c.get("/v1/sessions", params={"session_state": "running", "limit": 1}).json()["total"]
    assert scheduler["alive"] and not scheduler["degraded"], "this journey reads a healthy install"
    open_view(page, console_url, "primer", "system:dashboard")
    cards = page.get_by_test_id("nv-sys-health")
    expect(cards).to_be_visible(timeout=20_000)

    scheduler_card = cards.locator(".nv-health-card").filter(has=page.locator(".nv-health-k", has_text="scheduler"))
    expect(scheduler_card.locator(".nv-health-v")).to_have_text("alive", timeout=15_000)
    expect(scheduler_card.locator(".nv-health-sub")).to_have_text(scheduler["detail"] or "healthy")

    sessions_card = cards.locator(".nv-health-card").filter(has=page.locator(".nv-health-k", has_text="sessions active"))
    _wait_for_card_to_equal(page, base_url, sessions_card, params={"session_state": "running"})
    expect(sessions_card.locator(".nv-health-sub")).to_have_text("running now")


def _total(base_url: str, **params) -> int:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return c.get("/v1/sessions", params={**params, "limit": 1}).json()["total"]


def _wait_for_card_to_equal(page: Page, base_url: str, card, *, params: dict, not_params: dict | None = None) -> str:
    """The card's value equals the total of ``GET /v1/sessions?<params>``, with the total RE-READ on every attempt (a turn starting or ending elsewhere moves it between two reads)."""
    deadline = time.monotonic() + 25
    while True:
        expected = str(_total(base_url, **params))
        shown = card.locator(".nv-health-v").inner_text()
        other = str(_total(base_url, **not_params)) if not_params else None
        if shown == expected and shown != other:
            return shown
        assert time.monotonic() < deadline, f"the card says {shown!r}, GET /v1/sessions?{params} says {expected!r}" + (f" (the other count is {other!r})" if other is not None else "")
        page.wait_for_timeout(500)


@pytest.mark.ui_e2e
@pytest.mark.timeout(180)
def test_a_session_parked_on_a_tool_is_not_counted_as_running(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path, unique_suffix: str) -> None:
    """The discriminator: a session parked on ``ask_user`` has ``status=running`` and ``session_state=parked``, so the two counts differ and the card must follow the second."""
    registry, mock_base_url = mock_llm_lan

    async def park():
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            return await drive_park_on_tool(
                client, registry, mock_base_url, suffix=unique_suffix, tool="system__ask_user",
                tool_args={"prompt": "dashboard probe: please wait"}, root=tmp_path,
            )

    sid, _scenario, _parked = _run_to_completion(park())
    try:
        assert _total(base_url, status="running") > _total(base_url, session_state="running"), "precondition: the parked session is in the status count and not in the state count"
        open_view(page, console_url, "primer", "system:dashboard")
        cards = page.get_by_test_id("nv-sys-health")
        expect(cards).to_be_visible(timeout=20_000)
        sessions_card = cards.locator(".nv-health-card").filter(has=page.locator(".nv-health-k", has_text="sessions active"))
        _wait_for_card_to_equal(page, base_url, sessions_card, params={"session_state": "running"}, not_params={"status": "running"})
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            pending = c.get(f"/v1/sessions/{sid}/ask_user/pending")
            if pending.status_code == 200 and pending.json().get("tool_call_id"):
                c.post(f"/v1/sessions/{sid}/yields/{pending.json()['tool_call_id']}/cancel", json={"reason": "dashboard journey cleanup"})
