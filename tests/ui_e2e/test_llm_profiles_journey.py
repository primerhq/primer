"""The LLM Profiles page, end to end (Platform revamp Plan 03, Task 5; design 7.1 and 7.2).

The page's one action is "+ Aggregate" (there is no "New profile": a single profile comes from the Register provider overlay's import). These journeys
drive the real console against the real server:

* aggregate two seeded profiles, in an order picked with the Up button, with round robin and a mid-stream failover, and read the row back: the order
  shown is the order saved and the defaults the form did not touch are the model's; then edit the routing and read every other field back unchanged;
* edit a single profile's description and read every other field back unchanged, its ``config`` included (PUT is a full replace, so a form that
  rebuilds the body from what it draws would reset it: master Review Focus 1);
* set a single profile's reasoning to Off and its extended JSON, and read both back with every other field unchanged (Task 7: model calls apply a
  profile's ``config``, #756);
* Cancel leaves the grid and the URL as they were;
* on a phone, the Platform section reads "LLM Profiles" and an aggregate's row and fact sheet say what the desktop card says.

Each test seeds its own rows through the API (an Ollama provider at a port nothing listens on: no model is called) and deletes them in ``finally``:
the aggregates first, then the single profiles, then the provider. The unit-level pins are ``tests/ui/test_llm_profile_overlays.py``,
``tests/ui/test_llm_profiles_helpers.py`` and ``tests/ui/test_llm_profiles_page.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from playwright.sync_api import Page, expect

from tests._support.model_profiles import profile_id_for, seed_llm_provider_with, seed_profile_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_mobile_tab, open_view

pytestmark = smk("SMK-UI-03", status="partial")

BIG, SMALL = "big-model", "small-model"


def _seed_pair(base_url: str, provider_id: str) -> tuple[str, str]:
    """An Ollama provider with two models, 32768 and 8192 tokens; the ids of their two profiles."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": provider_id, "provider": "ollama", "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": BIG, "context_length": 32768}, {"name": SMALL, "context_length": 8192}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
    return profile_id_for(provider_id, BIG), profile_id_for(provider_id, SMALL)


def _clean_up(base_url: str, provider_id: str, aggregates: list[str], singles: list[str]) -> None:
    """The aggregates first (a member of an aggregate cannot be deleted), then the single profiles, then the provider."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for profile_id in [*aggregates, *singles]:
            c.delete(f"/v1/model_profiles/{profile_id}")
        c.delete(f"/v1/llm_providers/{provider_id}")


def _row(base_url: str, profile_id: str) -> dict:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get(f"/v1/model_profiles/{profile_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _row_when(base_url: str, profile_id: str, done: Callable[[dict], bool], *, timeout: float = 15.0) -> dict:
    """The stored row once ``done(row)`` holds (the save is a request the console sends after the click), or the last row read."""
    deadline = time.time() + timeout
    row = _row(base_url, profile_id)
    while not done(row) and time.time() < deadline:
        time.sleep(0.3)
        row = _row(base_url, profile_id)
    return row


def _open_profiles_page(page: Page, console_url: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation to
    the Studio view), so wait for it and navigate once more if the page is not the Platform one after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-plat-page:profiles")
    for attempt in (1, 2):
        open_view(page, console_url, default_wid(console_url), "platform:profiles")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def _open_card(page: Page, profile_id: str) -> None:
    page.get_by_test_id("nv-plat-filter").fill(profile_id)
    page.get_by_test_id(f"pk-card-open:{profile_id}").click()


def test_aggregate_a_pair_and_edit_its_routing(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"llm-lpj-{unique_suffix}"
    name = f"agg-lpj-{unique_suffix}"
    a, b = _seed_pair(base_url, provider_id)
    try:
        _open_profiles_page(page, console_url)
        expect(page.get_by_test_id("pk-header").get_by_role("heading")).to_have_text("LLM Profiles")
        create = page.get_by_test_id("nv-plat-create")
        expect(create).to_have_text("+ Aggregate")
        create.click()

        overlay = page.get_by_test_id("mp-aggregate")
        expect(overlay).to_contain_text("New aggregated profile", timeout=10_000)
        page.get_by_test_id("mp-aggregate-name").fill(name)
        page.get_by_test_id(f"mp-aggregate-members:{b}").check()
        page.get_by_test_id(f"mp-aggregate-members:{a}").check()
        overlay.get_by_role("button", name=f"Move {a} up").click()
        expect(page.get_by_test_id(f"mp-member-order:{a}")).to_have_text("1")
        expect(page.get_by_test_id(f"mp-member-order:{b}")).to_have_text("2")
        expect(page.get_by_test_id("mp-aggregate-window")).to_have_text("8,192 (the smallest of the picked profiles)")
        page.get_by_test_id("mp-aggregate-routing:round_robin").click()
        page.get_by_test_id("mp-aggregate-advanced:toggle").click()
        page.get_by_test_id("mp-aggregate-failover-point:mid_stream").click()
        page.get_by_test_id("mp-aggregate-submit").click()

        expect(page.get_by_test_id("nv-toasts")).to_contain_text(f"Created aggregated profile {name}", timeout=15_000)
        expect(overlay).to_have_count(0)
        page.get_by_test_id("nv-plat-filter").fill(name)
        card = page.get_by_test_id(f"pk-card:{name}")
        expect(card).to_contain_text("2 profiles · round robin", timeout=20_000)
        expect(page.get_by_test_id(f"pk-card-badge:{name}")).to_have_text("aggregated")
        expect(card).to_contain_text("8,192 tokens (smallest)")

        created = _row(base_url, name)
        assert created["members"] == [a, b], "the order shown is the order saved"
        assert created["strategy"] == "round_robin" and created["failover_point"] == "mid_stream"
        assert created["failover_on"] == "transient_and_config", "a choice the form was not asked to change is the model's default"
        assert created["description"] == f"Aggregate of {a}, {b}"

        page.get_by_test_id(f"pk-card-open:{name}").click()
        expect(overlay).to_contain_text("Edit aggregated profile", timeout=10_000)
        expect(page.get_by_test_id("mp-aggregate-name")).to_have_value(name)
        expect(page.get_by_test_id("mp-aggregate-name")).not_to_be_editable()
        page.get_by_test_id("mp-aggregate-routing:sequential").click()
        page.get_by_test_id("mp-aggregate-submit").click()
        expect(page.get_by_test_id("nv-toasts")).to_contain_text(f"Saved {name}", timeout=15_000)

        edited = _row_when(base_url, name, lambda row: row["strategy"] == "sequential")
        assert edited["strategy"] == "sequential", edited
        assert {k: v for k, v in edited.items() if k != "strategy"} == {k: v for k, v in created.items() if k != "strategy"}, "every other field is unchanged"
    finally:
        _clean_up(base_url, provider_id, [name], [a, b])


def test_editing_a_single_profile_keeps_its_settings(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"llm-lps-{unique_suffix}"
    a, b = _seed_pair(base_url, provider_id)
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        tuned = seed_profile_with(c, provider_id, "tuned-model", context_length=16384, config={"reasoning": "high", "extended": {"top_k": 20}})
    seeded = _row(base_url, tuned)
    assert seeded["config"] == {"reasoning": "high", "extended": {"top_k": 20}}, seeded
    try:
        _open_profiles_page(page, console_url)
        _open_card(page, tuned)
        expect(page.get_by_test_id("mp-single")).to_contain_text("Edit LLM profile", timeout=10_000)
        page.get_by_test_id("mp-single-description").fill("edited by the journey")
        page.get_by_test_id("mp-single-submit").click()
        expect(page.get_by_test_id("nv-toasts")).to_contain_text(f"Saved {tuned}", timeout=15_000)

        edited = _row_when(base_url, tuned, lambda row: row["description"] == "edited by the journey")
        assert edited["description"] == "edited by the journey", edited
        assert {k: v for k, v in edited.items() if k != "description"} == {k: v for k, v in seeded.items() if k != "description"}, (
            "PUT is a full replace: config (reasoning, extended) and every other field the form did not change must come back as stored")
    finally:
        _clean_up(base_url, provider_id, [], [tuned, a, b])


def test_setting_reasoning_off_reaches_the_row(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"llm-lpr-{unique_suffix}"
    a, b = _seed_pair(base_url, provider_id)
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        tuned = seed_profile_with(c, provider_id, "tuned-model", context_length=16384, config={"reasoning": "high", "extended": {"top_k": 20}})
    seeded = _row(base_url, tuned)
    try:
        _open_profiles_page(page, console_url)
        _open_card(page, tuned)
        expect(page.get_by_test_id("mp-single-settings")).to_contain_text("MODEL SETTINGS", timeout=10_000)
        expect(page.get_by_test_id("mp-single-reasoning:high")).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_test_id("mp-single-extended")).to_have_value('{\n  "top_k": 20\n}')
        page.get_by_test_id("mp-single-reasoning:off").click()
        page.get_by_test_id("mp-single-extended").fill('{"top_k": 5}')
        page.get_by_test_id("mp-single-submit").click()
        expect(page.get_by_test_id("nv-toasts")).to_contain_text(f"Saved {tuned}", timeout=15_000)

        edited = _row_when(base_url, tuned, lambda row: row["config"].get("reasoning") == "off")
        assert edited["config"] == {"reasoning": "off", "extended": {"top_k": 5}}, edited
        assert {k: v for k, v in edited.items() if k != "config"} == {k: v for k, v in seeded.items() if k != "config"}, "every other field is unchanged"
    finally:
        _clean_up(base_url, provider_id, [], [tuned, a, b])

def test_cancel_leaves_the_grid_and_the_url(page: Page, console_url: str) -> None:
    _open_profiles_page(page, console_url)
    before = page.url
    page.get_by_test_id("nv-plat-create").click()
    expect(page.get_by_test_id("mp-aggregate")).to_be_visible(timeout=10_000)
    page.get_by_test_id("mp-aggregate-cancel").click()
    expect(page.get_by_test_id("mp-aggregate")).to_have_count(0)
    expect(page.locator(".modal")).to_have_count(0)
    expect(page.get_by_test_id("nv-plat-page:profiles")).to_be_visible()
    assert page.url == before, "the overlay is page-local: opening and cancelling it does not touch the URL"


def test_the_phone_lists_llm_profiles(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"llm-lpm-{unique_suffix}"
    name = f"agg-lpm-{unique_suffix}"
    a, b = _seed_pair(base_url, provider_id)
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post("/v1/model_profiles", json={"id": name, "description": f"Aggregate of {a}, {b}", "kind": "aggregated", "members": [a, b]})
            assert r.status_code == 201, r.text
        page.set_viewport_size({"width": 390, "height": 844})
        open_mobile_tab(page, console_url, "more")
        nav = page.get_by_test_id("nv-mob-plat-nav:profiles")
        expect(nav).to_contain_text("LLM Profiles")
        nav.click()
        expect(page.get_by_test_id("nv-mob-plat-page:profiles")).to_be_visible(timeout=20_000)
        page.get_by_test_id("nv-mob-plat-filter").fill(name)

        row = page.get_by_test_id(f"nv-mob-plat-row:{name}")
        expect(row).to_contain_text("2 profiles · sequential fallback", timeout=15_000)
        row.click()
        sheet = page.get_by_test_id("nv-mob-fact-body")
        expect(sheet).to_contain_text("context window", timeout=10_000)
        expect(sheet).to_contain_text("8,192 tokens (smallest)")
        expect(sheet).to_contain_text("aggregated")
    finally:
        _clean_up(base_url, provider_id, [name], [a, b])
