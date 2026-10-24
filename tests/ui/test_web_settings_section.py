from __future__ import annotations

from pathlib import Path

from primer.model.web_fetch import RESERVED_WEB_FETCH_IDS
from primer.model.web_search import RESERVED_WEB_SEARCH_IDS
from tests.ui._web_settings_harness import (
    real_not_bootstrapped_envelope,
    real_unknown_ids_envelope,
    web_ui,
)
from tests.ui.test_platform_page_hooks import _returns_before_a_hook

ROOT = Path(__file__).resolve().parents[2]

DDG = {"id": "DuckDuckGo", "provider_type": "duckduckgo", "config": {"type": "duckduckgo"}}
TAV = {"id": "tavily-a", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "**********"}}
EXA = {"id": "exa-b", "provider_type": "exa", "config": {"type": "exa", "api_key": "**********"}}

# Suppress unused import warning for web_ui fixture
_ = web_ui


def test_single_mode_shows_the_default_provider_with_built_in_labels(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[TAV, DDG])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    seg = ui.kit("PK_Segmented", "web-search-mode")
    assert seg is not None
    assert seg["value"] == "single"
    assert seg["options"] == [{"value": "single", "label": "Single"}, {"value": "aggregated", "label": "Aggregated"}]

    default_el = ui.find("web-search-default")
    assert default_el is not None
    assert default_el["value"] == "DuckDuckGo"

    opts = [o["text"] for o in ui.options("web-search-default")]
    assert opts == ["DuckDuckGo (built in)", "tavily-a (Tavily)"]

    field = ui.kit("PK_Field", "Default provider")
    assert field is not None
    assert field["help"] == "Every web search goes to this provider."


def test_picking_another_provider_puts_single_mode_and_toasts(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[TAV, DDG])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.change("web-search-default", "tavily-a")
    assert ui.calls() == [["PUT", "/web_search_active_config", {"config": {"mode": "single", "provider_id": "tavily-a"}}]]
    assert ui.toasts() == [{"kind": "success", "title": "Web search updated", "detail": "Single: tavily-a (Tavily)"}]
    assert ui.refetched().get("web:search:active") == 1


def test_switching_to_aggregated_starts_the_list_with_the_current_default(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[TAV, DDG])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.call("PK_Segmented", "web-search-mode", "onChange", "aggregated")
    assert ui.calls() == [["PUT", "/web_search_active_config", {"config": {"mode": "aggregated", "provider_ids": ["DuckDuckGo"]}}]]


def test_the_ordered_list_puts_members_first_and_sends_the_new_order(web_ui) -> None:
    ui = web_ui()
    ui.set_resource(
        "web:search:active",
        data={"id": "active", "config": {"mode": "aggregated", "provider_ids": ["tavily-a", "DuckDuckGo"]}},
    )
    ui.set_resource("web:search:providers", data=[DDG, EXA, TAV])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    tbl = ui.kit("PK_CheckTable", "web-search-chain")
    assert tbl is not None
    rows = [(r["key"], r["label"], r["checked"]) for r in tbl["rows"]]
    assert rows == [
        ("tavily-a", "tavily-a (Tavily)", True),
        ("DuckDuckGo", "DuckDuckGo (built in)", True),
        ("exa-b", "exa-b (Exa)", False),
    ]
    assert tbl["extraLabel"] == "Order"

    up_tav = ui.find("web-search-up:tavily-a")
    down_ddg = ui.find("web-search-down:DuckDuckGo")
    assert up_tav is not None and up_tav["disabled"] is True
    assert down_ddg is not None and down_ddg["disabled"] is True

    ui.call("PK_CheckTable", "web-search-chain", "onToggle", "exa-b")
    assert ui.calls() == [
        ["PUT", "/web_search_active_config", {"config": {"mode": "aggregated", "provider_ids": ["tavily-a", "DuckDuckGo", "exa-b"]}}]
    ]

    # on a fresh mount click("web-search-up:DuckDuckGo") PUTs ["DuckDuckGo", "tavily-a"]
    ui2 = web_ui()
    ui2.set_resource(
        "web:search:active",
        data={"id": "active", "config": {"mode": "aggregated", "provider_ids": ["tavily-a", "DuckDuckGo"]}},
    )
    ui2.set_resource("web:search:providers", data=[DDG, EXA, TAV])
    ui2.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )
    ui2.click("web-search-up:DuckDuckGo")
    assert ui2.calls() == [
        ["PUT", "/web_search_active_config", {"config": {"mode": "aggregated", "provider_ids": ["DuckDuckGo", "tavily-a"]}}]
    ]

    field = ui.kit("PK_Field", "Provider order")
    assert field is not None
    assert field["help"] == "Providers are tried in this order. The next one is used when one fails."


def test_the_last_provider_cannot_be_unticked(web_ui) -> None:
    ui = web_ui()
    ui.set_resource(
        "web:search:active",
        data={"id": "active", "config": {"mode": "aggregated", "provider_ids": ["DuckDuckGo"]}},
    )
    ui.set_resource("web:search:providers", data=[DDG, TAV])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.call("PK_CheckTable", "web-search-chain", "onToggle", "DuckDuckGo")
    assert ui.calls() == []
    field = ui.kit("PK_Field", "Provider order")
    assert field is not None
    assert field["error"] == "Keep at least one provider. Switch to Single to use one provider."

    ui.call("PK_CheckTable", "web-search-chain", "onToggleAll", False)
    assert ui.calls() == []
    field = ui.kit("PK_Field", "Provider order")
    assert field is not None
    assert field["error"] == "Keep at least one provider. Switch to Single to use one provider."


def test_single_then_aggregated_restores_the_list_the_page_last_saw(web_ui) -> None:
    ui = web_ui()
    ui.set_resource(
        "web:search:active",
        data={"id": "active", "config": {"mode": "aggregated", "provider_ids": ["tavily-a", "DuckDuckGo"]}},
    )
    ui.set_resource("web:search:providers", data=[DDG, TAV, EXA])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.call("PK_Segmented", "web-search-mode", "onChange", "single")
    assert ui.calls() == [["PUT", "/web_search_active_config", {"config": {"mode": "single", "provider_id": "tavily-a"}}]]

    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "tavily-a"}})
    ui.rerender()

    ui.call("PK_Segmented", "web-search-mode", "onChange", "aggregated")
    assert ui.calls()[-1] == [
        "PUT",
        "/web_search_active_config",
        {"config": {"mode": "aggregated", "provider_ids": ["tavily-a", "DuckDuckGo"]}},
    ]

    # a remembered id removed from the rows is dropped from that list
    ui.set_resource("web:search:providers", data=[TAV, EXA])
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "tavily-a"}})
    ui.rerender()
    ui.call("PK_Segmented", "web-search-mode", "onChange", "aggregated")
    assert ui.calls()[-1] == [
        "PUT",
        "/web_search_active_config",
        {"config": {"mode": "aggregated", "provider_ids": ["tavily-a"]}},
    ]


def test_a_configured_id_missing_from_the_list_is_shown_not_replaced(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "gone"}})
    ui.set_resource("web:search:providers", data=[DDG])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    default_el = ui.find("web-search-default")
    assert default_el is not None
    assert default_el["value"] == "gone"

    opts = ui.options("web-search-default")
    assert opts[0]["text"] == "gone (missing)"

    field = ui.kit("PK_Field", "Default provider")
    assert field is not None
    assert field["error"] == "gone no longer exists. Pick another provider."

    # aggregated ["gone", "DuckDuckGo"]: the first chain row is ("gone", "gone (missing)", True)
    ui2 = web_ui()
    ui2.set_resource(
        "web:search:active",
        data={"id": "active", "config": {"mode": "aggregated", "provider_ids": ["gone", "DuckDuckGo"]}},
    )
    ui2.set_resource("web:search:providers", data=[DDG])
    ui2.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    tbl = ui2.kit("PK_CheckTable", "web-search-chain")
    assert tbl is not None
    r0 = tbl["rows"][0]
    assert (r0["key"], r0["label"], r0["checked"]) == ("gone", "gone (missing)", True)


def test_a_refused_save_shows_why_under_the_field_and_reloads_both_lists(web_ui) -> None:
    env = real_unknown_ids_envelope(["tavily-a"])
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[DDG, TAV])
    ui.answer("PUT /web_search_active_config", envelope=env)
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.change("web-search-default", "tavily-a")

    field = ui.kit("PK_Field", "Default provider")
    assert field is not None
    expected_msg = "No longer exists: tavily-a. The list has been reloaded."
    assert field["error"] == expected_msg

    req_id = (env.get("extensions") or {}).get("request_id")
    assert ui.toasts() == [{
        "kind": "error",
        "title": "Web search not saved",
        "detail": expected_msg,
        "requestId": req_id or None,
    }]
    ref = ui.refetched()
    assert ref.get("web:search:active") == 1
    assert ref.get("web:search:providers") == 1


def test_a_refused_mode_switch_says_why_under_mode(web_ui) -> None:
    """The reason of a refused save goes under the field that changed: a switch of Mode is refused under Mode, not under the default it did not touch, and the next
    save that is answered clears it."""
    env = real_unknown_ids_envelope(["tavily-a"])
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "tavily-a"}})
    ui.set_resource("web:search:providers", data=[DDG, TAV])
    ui.answer("PUT /web_search_active_config", envelope=env)
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.call("PK_Segmented", "web-search-mode", "onChange", "aggregated")

    expected_msg = "No longer exists: tavily-a. The list has been reloaded."
    assert ui.calls() == [["PUT", "/web_search_active_config", {"config": {"mode": "aggregated", "provider_ids": ["tavily-a"]}}]]
    assert ui.kit("PK_Field", "Mode").get("error") == expected_msg
    assert ui.kit("PK_Field", "Default provider").get("error") is None
    assert [t["title"] for t in ui.toasts()] == ["Web search not saved"]

    ui.answer("PUT /web_search_active_config", body={})
    ui.change("web-search-default", "DuckDuckGo")
    assert ui.kit("PK_Field", "Mode").get("error") is None


def test_a_change_while_a_save_is_out_is_ignored(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[DDG, TAV])
    ui.answer("PUT /web_search_active_config", hold=True)
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.change("web-search-default", "tavily-a")
    ui.call("PK_Segmented", "web-search-mode", "onChange", "aggregated")

    assert len(ui.calls()) == 1
    default_el = ui.find("web-search-default")
    assert default_el is not None
    assert default_el["aria-busy"] == "true"


def test_a_search_change_writes_only_the_search_config(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[DDG, TAV])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    ui.change("web-search-default", "tavily-a")
    assert ui.calls() == [["PUT", "/web_search_active_config", {"config": {"mode": "single", "provider_id": "tavily-a"}}]]


def test_the_manage_link_says_where_it_goes_and_calls_back(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[DDG])
    ui.ctx.eval("var MANAGE_CALLED = 0;")
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2, onManageProviders: function () { MANAGE_CALLED++; }}",
    )

    btn = ui.find("web-search-manage")
    assert btn is not None
    assert ui.js("textOf(MR.find('web-search-manage'))") == "Manage search providers in Providers \u203a Web Search \u2192"
    ui.click("web-search-manage")
    assert ui.js("MANAGE_CALLED") == 1


def test_the_fetch_section_uses_its_own_words_and_route(web_ui) -> None:
    ui = web_ui()
    jina_row = {"id": "jina-1", "provider_type": "jina", "config": {"type": "jina", "api_key": None}}
    local_row = {"id": "local", "provider_type": "local", "config": {"type": "local"}}
    ui.set_resource("web:fetch:active", data={"id": "active", "config": {"mode": "single", "provider_id": "local"}})
    ui.set_resource("web:fetch:providers", data=[jina_row, local_row])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'fetch', active: __resource('web:fetch:active'), providers: __resource('web:fetch:providers'), cols: 2}",
    )

    opts = [o["text"] for o in ui.options("web-fetch-default")]
    assert opts == ["Local (built in)", "jina-1 (Jina Reader)"]

    field = ui.kit("PK_Field", "Default fetcher")
    assert field is not None
    assert field["help"] == "Every page fetch goes to this fetcher."

    assert ui.find("web-search-manage") is None

    ui.change("web-fetch-default", "jina-1")
    assert ui.calls() == [["PUT", "/web_fetch_active_config", {"config": {"mode": "single", "provider_id": "jina-1"}}]]
    assert ui.toasts() == [{"kind": "success", "title": "Web fetch updated", "detail": "Single: jina-1 (Jina Reader)"}]


def test_loading_and_a_load_error_with_retry(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", loading=True)
    ui.set_resource("web:search:providers", data=[DDG])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    assert any("Loading web search settings\u2026" in t for t in ui.texts())
    assert ui.kit("PK_Segmented", "web-search-mode") is None

    env = real_not_bootstrapped_envelope()
    ui2 = web_ui()
    ui2.set_resource("web:search:active", error_envelope=env)
    ui2.set_resource("web:search:providers", data=[DDG])
    ui2.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 2}",
    )

    err_el = ui2.find("web-search-error")
    assert err_el is not None
    assert any("active web search config row is missing" in t for t in ui2.texts())

    ui2.click("web-search-retry")
    ref = ui2.refetched()
    assert ref.get("web:search:active") == 1
    assert ref.get("web:search:providers") == 1


def test_cols_reach_the_grid(web_ui) -> None:
    ui = web_ui()
    ui.set_resource("web:search:active", data={"id": "active", "config": {"mode": "single", "provider_id": "DuckDuckGo"}})
    ui.set_resource("web:search:providers", data=[DDG])
    ui.mount(
        "WEB_ActiveConfigSection",
        "{kind: 'search', active: __resource('web:search:active'), providers: __resource('web:search:providers'), cols: 1}",
    )

    grid = ui.kit("PK_Grid", "_")
    assert grid is not None
    assert grid["cols"] == 1


def test_the_built_in_ids_are_the_servers(web_ui) -> None:
    ui = web_ui()
    assert ui.js("WEB_KINDS.search.builtinId") == next(iter(RESERVED_WEB_SEARCH_IDS))
    assert ui.js("WEB_KINDS.fetch.builtinId") == next(iter(RESERVED_WEB_FETCH_IDS))


def test_every_hook_runs_before_an_early_return() -> None:
    src = (ROOT / "ui" / "components" / "web-settings.jsx").read_text(encoding="utf-8")
    assert _returns_before_a_hook(src) == []
