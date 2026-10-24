"""The LLM Profiles page entry (Platform revamp Plan 03, Task 4; design 7.1).

The ``profiles`` entry of ``NV_PLAT_PAGES`` (``ui/components/console/nv-platform.jsx``) is the page's whole contract with the generic Platform list page:
its title, its noun, its one action ("+ Aggregate", there is no "New profile": a single profile comes from the Register provider overlay's import), the
card of each row, the list it asks for and the DELETE route of a card. The card and the list annotation are plain-JS helpers of
``ui/components/model-profiles.jsx`` (``MP_profileCard``, ``MP_withWindows``), so this file evaluates, in one bare V8 context: ``ui/foundation/api.js``,
the helper block of ``model-profiles.jsx`` exactly as written, and the JSX-free stretch of ``nv-platform.jsx`` that holds the table (the slice
``tests/ui/test_platform_create_opens_the_form.py`` runs).

What it pins:

* a single profile's card says "imported", its provider and model, its window and who uses it; an aggregated one says "aggregated", how many profiles it
  chains and how, and the SMALLEST window of its members, which the list works out from the rows it loaded (the server computes the same minimum only
  when it resolves the profile, and serves it on no route), or "unknown" when a member is not among them, never a guess;
* every fact is a ``[label, value]`` pair of strings, at most three: the phone's fact sheet draws ``f[0]`` and ``f[1]``;
* a harness-managed profile has no Delete (the server answers 409) and says who manages it.

The desktop page draws the card through ``PK_Card``; the last tests mount the real page (``tests/ui/_platform_page_harness.py``) to see the badge and the
menu the card view model turns into.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._platform_page_harness import platform_page_context

ROOT = Path(__file__).resolve().parents[2]
MODEL_PROFILES_SRC = (ROOT / "ui" / "components" / "model-profiles.jsx").read_text(encoding="utf-8")
PLATFORM_SRC = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
API = ROOT / "ui" / "foundation" / "api.js"

START = "// ---- MP helpers: plain JS, no JSX (tests/ui/test_llm_profiles_helpers.py evaluates this block) ----"
END = "// ---- end MP helpers ----"

A = {"id": "vllm-gx10--qwen3.8-27b", "kind": "single", "provider_id": "vllm-gx10", "model_name": "qwen3.8-27b",
     "context_length": 262144, "agent_count": 2, "graph_node_count": 1, "harness_id": None}
B = {"id": "openrouter--claude", "kind": "single", "provider_id": "openrouter", "model_name": "anthropic/claude",
     "context_length": 200000, "agent_count": 0, "graph_node_count": 0, "harness_id": None}
AGG = {"id": "fast-with-fallback", "kind": "aggregated", "members": [A["id"], B["id"]], "strategy": "sequential",
       "agent_count": 1, "graph_node_count": 0, "harness_id": None}


def helper_block() -> str:
    start = MODEL_PROFILES_SRC.index(START) + len(START)
    return MODEL_PROFILES_SRC[start : MODEL_PROFILES_SRC.index(END, start)]


def profiles_entry_source() -> str:
    start = PLATFORM_SRC.index("\n  profiles: {\n")
    return PLATFORM_SRC[start : PLATFORM_SRC.index("\n  agents: {\n", start)]


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    try:
        c.eval("var window = globalThis;")
        c.eval(API.read_text(encoding="utf-8"))
        c.eval(helper_block())
        c.eval(PLATFORM_SRC[PLATFORM_SRC.index("function NV_fact(") : PLATFORM_SRC.index("var NV_PLAT_PAGE_SIZE")])
        c.eval("var MODALS = [], CONSOLE = {};"
               "function setModal(m) { MODALS.push(m); }")
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval(f"(function () {{ var v = ({expr}); return JSON.stringify(v === undefined ? null : v); }})()"))


def card(ctx, row: dict) -> dict:
    return js(ctx, f"NV_PLAT_PAGES.profiles.card({json.dumps(row)}, CONSOLE)")


def list_items(ctx, rows: list[dict]) -> list[dict]:
    """Run the entry's ``list`` with an ``apiFetch`` that answers ``{items: rows, total}``; the promise settles at the end of the first eval."""
    ctx.eval(f"var LIST_CALLS = [], LIST_ANSWER = {{ items: {json.dumps(rows)}, total: {len(rows)} }}, LIST_RESULT = null;"
             "NV_PLAT_PAGES.profiles.list(function (method, path, body, opts) {"
             "  LIST_CALLS.push([method, path]); return Promise.resolve(LIST_ANSWER);"
             "}, null).then(function (r) { LIST_RESULT = r; });")
    result = js(ctx, "LIST_RESULT")
    assert result is not None, "the list promise did not settle"
    return result["items"]


# ---- the page --------------------------------------------------------------------------------------------------------------------------------------


def test_the_page_is_llm_profiles_and_its_action_is_aggregate(ctx) -> None:
    got = js(ctx, "[NV_PLAT_PAGES.profiles.title, NV_PLAT_PAGES.profiles.noun, NV_PLAT_PAGES.profiles.createLabel]")
    assert got == ["LLM Profiles", ["profile", "profiles"], "+ Aggregate"]


def test_no_new_profile_button_is_left() -> None:
    assert "New profile" not in MODEL_PROFILES_SRC and "New profile" not in profiles_entry_source()


def test_create_and_open_still_host_the_profile_form(ctx) -> None:
    ctx.eval("NV_PLAT_PAGES.profiles.create(CONSOLE, setModal);")
    ctx.eval(f"NV_PLAT_PAGES.profiles.open(CONSOLE, {json.dumps(A)}, setModal);")
    assert js(ctx, "MODALS") == [{"kind": "profile", "row": None}, {"kind": "profile", "row": A}]


# ---- the cards -------------------------------------------------------------------------------------------------------------------------------------


def test_a_single_card_shows_imported_provider_model_window_and_usage(ctx) -> None:
    assert card(ctx, A) == {
        "id": A["id"], "name": A["id"], "sub": "vllm-gx10 · qwen3.8-27b", "badge": "imported",
        "icon": {"path": js(ctx, "MP_ICON_SINGLE"), "tone": "var(--text-2)"},
        "facts": [["context window", "262,144 tokens"], ["usage", "bound by 2 agents · 1 graph node"]],
    }


def test_an_aggregated_card_shows_size_routing_and_smallest_window(ctx) -> None:
    items = list_items(ctx, [A, B, AGG])
    agg = card(ctx, items[2])
    assert agg["sub"] == "2 profiles · sequential fallback" and agg["badge"] == "aggregated"
    assert agg["icon"] == {"path": js(ctx, "MP_ICON_AGGREGATED"), "tone": "var(--accent)"}
    assert agg["facts"] == [["context window", "200,000 tokens (smallest)"], ["usage", "bound by 1 agent"]]
    assert card(ctx, {**items[2], "strategy": "round_robin"})["sub"] == "2 profiles · round robin"


def test_the_two_icons_are_different_paths(ctx) -> None:
    single, aggregated = js(ctx, "[MP_ICON_SINGLE, MP_ICON_AGGREGATED]")
    assert single and aggregated and single != aggregated


def test_an_aggregate_whose_member_is_not_listed_shows_an_unknown_window(ctx) -> None:
    """The list is capped at 200 profiles: a member it does not hold has no window the console can read, and the card says so instead of guessing."""
    items = list_items(ctx, [A, AGG])
    assert card(ctx, items[1])["facts"][0] == ["context window", "unknown"]


def test_an_aggregate_that_was_not_annotated_shows_an_unknown_window(ctx) -> None:
    """A row that did not come through the entry's list (no ``_window``) is not given a window either."""
    assert card(ctx, AGG)["facts"][0] == ["context window", "unknown"]


def test_usage_says_unbound_at_zero(ctx) -> None:
    assert ["usage", "unbound"] in card(ctx, B)["facts"]
    assert ["usage", "unbound"] in card(ctx, {"id": "bare"})["facts"], "a row the list route did not count is not counted as used"


def test_a_managed_profile_has_no_delete_and_says_who_manages_it(ctx) -> None:
    managed = {**A, "harness_id": "h"}
    assert js(ctx, f"NV_PLAT_PAGES.profiles.delPath({json.dumps(managed)})") is None
    assert card(ctx, managed)["facts"][-1] == ["managed by", "h"]
    assert js(ctx, f"NV_PLAT_PAGES.profiles.delPath({json.dumps(A)})") == "/model_profiles/vllm-gx10--qwen3.8-27b"
    assert js(ctx, 'NV_PLAT_PAGES.profiles.delPath({ id: "a b" })') == "/model_profiles/a%20b"
    assert ["managed by", "h"] not in card(ctx, A)["facts"] + card(ctx, {**A, "harness_id": ""})["facts"]


def test_every_fact_is_a_label_value_pair_of_strings(ctx) -> None:
    items = list_items(ctx, [A, B, AGG])
    for row in [*items, {**A, "harness_id": "h"}, {**items[2], "harness_id": "h"}, {"id": "bare"}, {"id": "bare-agg", "kind": "aggregated"}]:
        facts = card(ctx, row)["facts"]
        assert 1 <= len(facts) <= 3, (row["id"], facts)
        for fact in facts:
            assert isinstance(fact, list) and len(fact) == 2 and all(isinstance(s, str) and s for s in fact), (row["id"], fact)


def test_a_card_never_reads_the_retired_reasoning_fact(ctx) -> None:
    """The old card drew a "reasoning" fact; design 7.1's card is the window and the usage, and the reasoning level lives in the profile's overlay."""
    assert not [f for f in card(ctx, {**A, "config": {"reasoning": "high"}})["facts"] if f[0] == "reasoning"]


# ---- the list ----------------------------------------------------------------------------------------------------------------------------------------


def test_list_annotates_aggregates_and_keeps_the_response(ctx) -> None:
    items = list_items(ctx, [A, B, AGG])
    assert js(ctx, "LIST_CALLS") == [["GET", "/model_profiles?limit=200"]]
    assert js(ctx, "LIST_RESULT.total") == 3
    assert [r.get("_window", "absent") for r in items] == ["absent", "absent", 200000], "only the aggregate is annotated"
    assert items[:2] == [A, B]
    assert js(ctx, "LIST_ANSWER") == {"items": [A, B, AGG], "total": 3}, "the response the fetch answered is not changed"


def test_with_windows_passes_an_answer_with_no_items_through(ctx) -> None:
    assert js(ctx, "MP_withWindows(null)") is None
    assert js(ctx, "MP_withWindows({ total: 0 })") == {"total": 0}
    assert js(ctx, "MP_withWindows({ items: [] })") == {"items": []}


# ---- the page that draws them ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def page_ctx():
    c = platform_page_context()
    try:
        yield c
    finally:
        c.close()


def test_the_desktop_cards_draw_the_badge_and_a_managed_card_has_no_menu(page_ctx) -> None:
    rows = [A, AGG, {**B, "harness_id": "h1"}]
    page_ctx.eval(f"CON.view = {{ name: 'platform', nav: 'profiles', sub: null }}; ITEMS = {json.dumps(rows)};"
                  " MR.mount(NV_PlatListPage, { nav: 'profiles' });")
    assert js(page_ctx, f"MR.find('pk-card-badge:{A['id']}').props.children") == "imported"
    assert js(page_ctx, f"MR.find('pk-card-badge:{AGG['id']}').props.children") == "aggregated"
    assert page_ctx.eval(f"!!MR.find('pk-card-menu:{A['id']}')"), "an unmanaged profile can be deleted from its card's menu"
    assert page_ctx.eval(f"MR.find('pk-card-menu:{B['id']}') === null"), "a harness-managed profile offers no Delete: the server answers 409"
    assert "managed by" in js(page_ctx, "MR.texts()")


def test_the_entry_hands_its_card_and_list_to_the_helpers() -> None:
    """One definition of the card: the entry calls the helpers rather than keeping a second mapping that could drift from the overlays'."""
    entry = profiles_entry_source()
    assert re.search(r"card: function \(row\) \{ return window\.MP_profileCard\(row\); \}", entry), entry
    assert "window.MP_withWindows" in entry


# ---- what the file exports (Task 6) ------------------------------------------------------------------------------------------------------------------


def test_model_profiles_exports_only_the_overlays() -> None:
    """The profile card, grid and all-profiles panel were mounted by the Providers page's profile panels, which left with Plan 02 (#804); the LLM
    Profiles page draws its cards through the kit (MP_profileCard). The legacy single create (``prefill``) was the Providers page's "New profile"."""
    exported = set(re.findall(r"^window\.(MP_\w+) = ", MODEL_PROFILES_SRC, re.M))
    assert exported == {"MP_ProfileModal", "MP_AggregateOverlay"}
    for gone in ("function MP_ProfileCard(", "function MP_ProfilesGrid(", "function MP_AllProfilesPanel(", "function MP_BoundBadge(",
                 "function MP_KindChip(", "function MP_ReasoningChip(", "MP_REASONING_LEVELS", "PC_ProfilesPanel", "prefill"):
        assert gone not in MODEL_PROFILES_SRC, gone
