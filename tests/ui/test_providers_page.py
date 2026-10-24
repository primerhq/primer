"""Unit tests for the one kit Providers page (plan 02, Task 20).

Tests the rewrite of ui/components/provider-catalog.jsx against the Plan 01 kit:
- All view shows cards from every nav class; a type view shows only its own
- Web fetchers never appear (web_fetch has nav: false)
- Header has PK_PageHeader with filter, pager and + Register action
- Page size is twelve (PK_usePagedFilter)
- Filter matches id, type and kind
- Built-ins carry a badge and no Delete menu
- Delete lives in the card menu behind confirmDialog
- A refused delete toasts the refusal reason
- Open renders in place for panel classes (ssp, workspace, channel)
- Open opens PC_RegisterOverlay in edit mode for non-panel classes
- Extras slot renders above that type's cards (tts gets rows; stub renders; none on all)
- A failed class shows Banner error with Retry while other classes still load
- More than 200 rows displays the truncation notice
- An instance deep link opens its row
- The provider.register verb is registered in nv-shell.jsx
- The provider.register verb requests the overlay
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled

CATALOG_FILE = COMPONENTS / "provider-catalog.jsx"
NV_SHELL_FILE = COMPONENTS / "console" / "nv-shell.jsx"

STUBS = r"""
var NAVIGATED = [];
function onNavigate(nav) {
  NAVIGATED.push(nav);
}

window.SSPDetail = function (props) {
  return el("div", { "data-testid": "ssp-detail" }, "SSPDetail:" + props.sspId);
};
window.WorkspaceProviderDetail = function (props) {
  return el("div", { "data-testid": "wp-detail" }, "WPDetail:" + props.providerId);
};
window.ChannelProviderDetail = function (props) {
  return el("div", { "data-testid": "ch-detail" }, "CHDetail:" + props.providerId);
};

var PROVIDER_FIXTURES = {};
var FAIL_CLASSES = {};
window.__FAIL_DELETE = null;

HANDLER = function (method, path, body) {
  var p = path.split("?")[0];
  if (method === "GET") {
    if (FAIL_CLASSES[p]) {
      return refuse(FAIL_CLASSES[p]);
    }
    if (p === "/model_profiles") {
      return Promise.resolve(PROVIDER_FIXTURES["/model_profiles"] || { items: [] });
    }
    if (PROVIDER_FIXTURES[p]) {
      return Promise.resolve(PROVIDER_FIXTURES[p]);
    }
    return Promise.resolve({ items: [] });
  }
  if (method === "DELETE") {
    if (window.__FAIL_DELETE) {
      return refuse(window.__FAIL_DELETE);
    }
    return Promise.resolve({ ok: true });
  }
  return Promise.resolve(body == null ? { ok: true } : JSON.parse(JSON.stringify(body)));
};
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code(with_overlay=True) + "\n" + transpiled(CATALOG_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def test_all_shows_every_type_and_a_type_shows_only_its_own(ctx) -> None:
    ctx.eval("""
    PROVIDER_FIXTURES["/llm_providers"] = { items: [{ id: "llm-1", provider: "anthropic" }] };
    PROVIDER_FIXTURES["/embedding_providers"] = { items: [{ id: "emb-1", provider: "openai", models: [{ name: "text-emb-3" }] }] };
    PROVIDER_FIXTURES["/ssp"] = { items: [{ id: "ssp-1", provider: "pgvector", config: { hostname: "pg", database: "db" } }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "all",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    all_cards = json.loads(ctx.eval("JSON.stringify(MR.findAll('pk-card:').map(function (n) { return n.props['data-testid']; }))"))
    assert any("pk-card:llm:llm-1" == c for c in all_cards), all_cards
    assert any("pk-card:embedding:emb-1" == c for c in all_cards), all_cards
    assert any("pk-card:ssp:ssp-1" == c for c in all_cards), all_cards

    ctx.eval("""
    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    llm_cards = json.loads(ctx.eval("JSON.stringify(MR.findAll('pk-card:').map(function (n) { return n.props['data-testid']; }))"))
    assert any("pk-card:llm:llm-1" == c for c in llm_cards)
    assert not any("pk-card:embedding:emb-1" == c for c in llm_cards)
    assert not any("pk-card:ssp:ssp-1" == c for c in llm_cards)


def test_web_fetchers_never_appear(ctx) -> None:
    ctx.eval("""
    PROVIDER_FIXTURES["/web_fetch_providers"] = { items: [{ id: "fetch-1", provider_type: "local" }] };
    MR.mount(window.ProviderCatalog, {
      initialClass: "all",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    fetch_calls = [c for c in calls if "web_fetch_providers" in c["path"]]
    assert len(fetch_calls) == 0, "web_fetch must not be queried"
    cards = json.loads(ctx.eval("JSON.stringify(MR.findAll('pk-card:').map(function (n) { return n.props['data-testid']; }))"))
    assert not any("web_fetch" in c for c in cards)


def test_the_header_has_filter_pager_and_register(ctx) -> None:
    ctx.eval("""
    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    assert ctx.eval("MR.find('pc-filter') !== null")
    assert ctx.eval("MR.find('pk-pager') !== null")
    assert ctx.eval("MR.find('pc-register-open') !== null")


def test_page_size_is_twelve(ctx) -> None:
    ctx.eval("""
    var items = [];
    for (var i = 1; i <= 15; i++) {
      items.push({ id: "llm-" + i, provider: "anthropic" });
    }
    PROVIDER_FIXTURES["/llm_providers"] = { items: items };

    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    range_text = ctx.eval("textOf(MR.find('pk-pager-range'))")
    assert "1-12 of 15" in range_text
    page1_cards = ctx.eval("MR.findAll('pk-card:').length")
    assert page1_cards == 12

    # Click next
    ctx.eval("press('pk-pager-next'); MR.rerender();")
    settle(ctx)
    range_text2 = ctx.eval("textOf(MR.find('pk-pager-range'))")
    assert "13-15 of 15" in range_text2
    page2_cards = ctx.eval("MR.findAll('pk-card:').length")
    assert page2_cards == 3


def test_the_filter_matches_id_type_and_kind(ctx) -> None:
    ctx.eval("""
    PROVIDER_FIXTURES["/llm_providers"] = { items: [{ id: "my-alpha-prod", provider: "anthropic" }] };
    PROVIDER_FIXTURES["/embedding_providers"] = { items: [{ id: "vector-emb", provider: "openai" }] };
    PROVIDER_FIXTURES["/ssp"] = { items: [{ id: "pg-store", provider: "pgvector", config: { hostname: "pg", database: "mydb" } }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "all",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    # Filter by id substring
    ctx.eval("setValue('pc-filter', 'alpha');")
    settle(ctx)
    cards = json.loads(ctx.eval("JSON.stringify(MR.findAll('pk-card:').map(function (n) { return n.props['data-testid']; }))"))
    assert len(cards) == 1 and "my-alpha-prod" in cards[0]

    # Filter by klass label
    ctx.eval("setValue('pc-filter', 'Semantic');")
    settle(ctx)
    cards = json.loads(ctx.eval("JSON.stringify(MR.findAll('pk-card:').map(function (n) { return n.props['data-testid']; }))"))
    assert len(cards) == 1 and "pg-store" in cards[0]

    # Filter by kind label
    ctx.eval("setValue('pc-filter', 'Anthropic');")
    settle(ctx)
    cards = json.loads(ctx.eval("JSON.stringify(MR.findAll('pk-card:').map(function (n) { return n.props['data-testid']; }))"))
    assert len(cards) == 1 and "my-alpha-prod" in cards[0]


def test_built_ins_have_a_badge_and_no_menu(ctx) -> None:
    ctx.eval("""
    PROVIDER_FIXTURES["/web_search_providers"] = { items: [{ id: "DuckDuckGo", provider: "duckduckgo" }] };
    PROVIDER_FIXTURES["/workspace_providers"] = { items: [{ id: "local", provider: "local", config: { root_path: "/work" } }] };
    PROVIDER_FIXTURES["/artifact_storage_providers"] = { items: [{ id: "artifact-storage-default", provider: "db" }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "all",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    badge = ctx.eval("textOf(MR.find('pk-card-badge:web_search:DuckDuckGo'))")
    assert "built in" in badge
    assert ctx.eval("MR.find('pk-card-menu:web_search:DuckDuckGo') === null"), "built-in must have no menu button"


def test_delete_lives_in_the_card_menu_behind_a_confirm(ctx) -> None:
    ctx.eval("""
    CONFIRMS = [];
    CONFIRM_ANSWER = true;
    CALLS = [];
    PROVIDER_FIXTURES["/llm_providers"] = { items: [{ id: "custom-llm", provider: "anthropic" }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    # Open menu and click delete
    ctx.eval("press('pk-card-menu:llm:custom-llm'); MR.rerender();")
    ctx.eval("press('pc-card-delete:llm:custom-llm');")
    settle(ctx)
    confirms = js(ctx, "CONFIRMS")
    assert len(confirms) == 1
    assert "Delete custom-llm?" in confirms[0]["title"]
    assert "LLM" in confirms[0]["message"]
    assert confirms[0]["danger"] is True

    calls = js(ctx, "CALLS")
    delete_calls = [c for c in calls if c["method"] == "DELETE" and "custom-llm" in c["path"]]
    assert len(delete_calls) == 1
    toasts = js(ctx, "TOASTS")
    assert any("Provider deleted" in (t.get("title") or "") for t in toasts)


def test_a_refused_delete_toasts_the_reason(ctx) -> None:
    envelope = {
        "type": "/errors/conflict",
        "title": "Conflict",
        "status": 409,
        "detail": "provider is in use by 3 agents",
        "instance": "/v1/llm_providers/used-llm",
    }
    ctx.eval(f"""
    CONFIRM_ANSWER = true;
    TOASTS = [];
    window.__FAIL_DELETE = {json.dumps(envelope)};
    PROVIDER_FIXTURES["/llm_providers"] = {{ items: [{{ id: "used-llm", provider: "anthropic" }}] }};

    MR.mount(window.ProviderCatalog, {{
      initialClass: "llm",
      onNavigate: onNavigate
    }});
    """)
    settle(ctx)
    ctx.eval("press('pk-card-menu:llm:used-llm'); MR.rerender();")
    ctx.eval("press('pc-card-delete:llm:used-llm');")
    settle(ctx)
    toasts = js(ctx, "TOASTS")
    error_toasts = [t for t in toasts if t.get("kind") == "error"]
    assert len(error_toasts) >= 1
    assert error_toasts[0]["title"] == "Delete refused"
    assert "provider is in use by 3 agents" in error_toasts[0]["detail"]


def test_open_shows_the_detail_page_for_panel_classes(ctx) -> None:
    ctx.eval("""
    NAVIGATED = [];
    PROVIDER_FIXTURES["/ssp"] = { items: [{ id: "vec-store-1", provider: "pgvector", config: { hostname: "localhost", database: "vectordb" } }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "ssp",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    ctx.eval("press('pk-card-open:ssp:vec-store-1'); MR.rerender();")
    settle(ctx)
    nav = js(ctx, "NAVIGATED")
    assert any(n.get("kind") == "provider-instance" and n.get("id") == "vec-store-1" for n in nav)
    detail = ctx.eval("MR.find('ssp-detail')")
    assert detail is not None
    assert "vec-store-1" in ctx.eval("textOf(MR.find('ssp-detail'))")


def test_saving_a_panel_provider_opens_its_detail(ctx) -> None:
    ctx.eval("""
    NAVIGATED = [];
    window.__REGISTER_PROPS = null;
    window.PC_RegisterOverlay = function (props) {
      window.__REGISTER_PROPS = props;
      return el("div", { "data-testid": "register-stub" }, "Register");
    };

    MR.mount(window.ProviderCatalog, {
      initialClass: "workspace",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    ctx.eval("press('pc-register-open'); MR.rerender();")
    assert ctx.eval("MR.find('register-stub') !== null")

    ctx.eval("""
    window.__REGISTER_PROPS.onSaved({ id: 'made-workspace', provider: 'local' });
    window.__REGISTER_PROPS.onClose();
    MR.rerender();
    """)
    settle(ctx)
    nav = js(ctx, "NAVIGATED")
    assert nav[-1] == {"kind": "provider-instance", "classKey": "workspace", "id": "made-workspace"}
    assert "made-workspace" in ctx.eval("textOf(MR.find('wp-detail'))")


def test_a_type_page_registers_only_that_provider_class(ctx) -> None:
    """A panel-class page cannot register an unrelated row and then open it
    through the panel class's detail component."""
    ctx.eval("""
    window.__REGISTER_PROPS = null;
    window.PC_RegisterOverlay = function (props) {
      window.__REGISTER_PROPS = props;
      return el("div", { "data-testid": "register-stub" }, "Register");
    };
    MR.mount(window.ProviderCatalog, {
      initialClass: "ssp",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    ctx.eval("press('pc-register-open'); MR.rerender();")
    assert js(ctx, "window.__REGISTER_PROPS.allowedClasses") == ["ssp"]


def test_closing_the_register_overlay_refetches_the_list(ctx) -> None:
    """Dismissal can race a completed import, so close refreshes even when
    the overlay did not reach onSaved."""
    ctx.eval("""
    CALLS = [];
    window.__REGISTER_PROPS = null;
    window.PC_RegisterOverlay = function (props) {
      window.__REGISTER_PROPS = props;
      return el("div", { "data-testid": "register-stub" }, "Register");
    };
    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    before = len([
        call for call in js(ctx, "CALLS")
        if call["method"] == "GET" and call["path"].startswith("/llm_providers")
    ])
    ctx.eval("press('pc-register-open'); MR.rerender();")
    ctx.eval("window.__REGISTER_PROPS.onClose(); MR.rerender();")
    settle(ctx)
    after = len([
        call for call in js(ctx, "CALLS")
        if call["method"] == "GET" and call["path"].startswith("/llm_providers")
    ])
    assert after == before + 1


def test_open_edits_every_other_class_in_the_overlay(ctx) -> None:
    ctx.eval("""
    NAVIGATED = [];
    PROVIDER_FIXTURES["/llm_providers"] = { items: [{ id: "my-llm", provider: "anthropic" }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    ctx.eval("press('pk-card-open:llm:my-llm'); MR.rerender();")
    settle(ctx)
    nav = js(ctx, "NAVIGATED")
    assert any(n.get("kind") == "provider-instance" and n.get("id") == "my-llm" for n in nav)
    assert ctx.eval("MR.find('pc-register') !== null")


def test_the_extras_slot_renders_above_that_types_cards(ctx) -> None:
    ctx.eval("""
    window.PC_TYPE_EXTRAS = window.PC_TYPE_EXTRAS || {};
    var TTS_EXTRAS_SEEN_ROWS = null;
    window.PC_TYPE_EXTRAS.tts = function (props) {
      TTS_EXTRAS_SEEN_ROWS = props.rows;
      return el("div", { "data-testid": "stub-tts-extras" }, "TTS Extras");
    };
    window.PC_TYPE_EXTRAS.artifact_storage = function () {
      return el("div", { "data-testid": "stub-artifact-extras" }, "Artifact Extras");
    };

    PROVIDER_FIXTURES["/tts_providers"] = { items: [{ id: "tts-1", provider: "openai" }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "tts",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    assert ctx.eval("MR.find('pc-extras:tts') !== null")
    assert ctx.eval("MR.find('stub-tts-extras') !== null")
    assert ctx.eval("TTS_EXTRAS_SEEN_ROWS && TTS_EXTRAS_SEEN_ROWS.length") == 1

    # On "all", no extras render
    ctx.eval("""
    MR.mount(window.ProviderCatalog, {
      initialClass: "all",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    assert ctx.eval("MR.find('pc-extras:all') === null")
    assert ctx.eval("MR.find('stub-tts-extras') === null")


def test_the_providers_page_has_no_web_search_chip(ctx) -> None:
    """Plan 02 Task 22: the web search mode and default live on the Web page (web-settings.jsx), so Providers > Web Search draws its cards and nothing above
    them: no extras slot, no old active-provider panel, and no read of /web_search_active_config. The file names neither the panel nor the route."""
    src = CATALOG_FILE.read_text(encoding="utf-8")
    assert "/web_search_active_config" not in src
    assert "PC_ActiveWebSearchPanel" not in src
    ctx.eval("""
    PROVIDER_FIXTURES["/web_search_providers"] = { items: [{ id: "DuckDuckGo", provider_type: "duckduckgo" }, { id: "tavily-1", provider_type: "tavily" }] };
    MR.mount(window.ProviderCatalog, {
      initialClass: "web_search",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    assert ctx.eval("MR.find('pk-card:web_search:DuckDuckGo') !== null")
    assert ctx.eval("MR.find('pk-card:web_search:tavily-1') !== null")
    assert ctx.eval("MR.find('pc-extras:web_search') === null"), "nothing is drawn above the Web Search cards"
    assert ctx.eval("MR.find('active-web-search-config') === null")
    paths = [c["path"] for c in js(ctx, "CALLS")]
    assert not [p for p in paths if "web_search_active_config" in p], paths


def test_a_failed_class_shows_its_error_and_the_others_still_load(ctx) -> None:
    envelope = {"type": "/errors/internal", "title": "Internal Error", "status": 500, "detail": "db failed"}
    ctx.eval(f"""
    FAIL_CLASSES["/llm_providers"] = {json.dumps(envelope)};
    PROVIDER_FIXTURES["/embedding_providers"] = {{ items: [{{ id: "emb-ok", provider: "openai" }}] }};

    MR.mount(window.ProviderCatalog, {{
      initialClass: "all",
      onNavigate: onNavigate
    }});
    """)
    settle(ctx)
    err = ctx.eval("MR.find('pc-error:llm')")
    assert err is not None
    assert "Could not load LLM providers" in ctx.eval("textOf(MR.find('pc-error:llm'))")
    # embedding still loaded
    assert ctx.eval("MR.find('pk-card:embedding:emb-ok') !== null")


def test_more_than_200_rows_says_so(ctx) -> None:
    ctx.eval("""
    var items = [];
    for (var i = 0; i < 200; i++) items.push({ id: "llm-" + i, provider: "anthropic" });
    PROVIDER_FIXTURES["/llm_providers"] = { items: items };

    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    trunc = ctx.eval("MR.find('pc-truncated:llm')")
    assert trunc is not None
    assert "Showing the first 200 LLM providers" in ctx.eval("textOf(MR.find('pc-truncated:llm'))")


def test_an_instance_deep_link_opens_its_row(ctx) -> None:
    ctx.eval("""
    PROVIDER_FIXTURES["/llm_providers"] = { items: [{ id: "deep-llm", provider: "anthropic" }] };

    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      initialInstanceId: "deep-llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    assert ctx.eval("MR.find('pc-register') !== null")


def test_the_register_verb_is_registered_in_nv_shell() -> None:
    shell_src = NV_SHELL_FILE.read_text(encoding="utf-8")
    assert 'id: "provider.register"' in shell_src
    assert 'label: "Register Provider"' in shell_src
    assert 'surfaces: ["palette", "platform-nav"]' in shell_src
    assert 'goView("platform", "providers")' in shell_src
    assert "window.PC_requestRegister(null)" in shell_src or "window.PC_requestRegister(" in shell_src


def test_the_register_verb_requests_the_overlay(ctx) -> None:
    ctx.eval("""
    MR.mount(window.ProviderCatalog, {
      initialClass: "llm",
      onNavigate: onNavigate
    });
    """)
    settle(ctx)
    assert ctx.eval("MR.find('pc-register') === null")
    # Trigger register request
    ctx.eval("window.PC_requestRegister(null); MR.rerender();")
    settle(ctx)
    assert ctx.eval("MR.find('pc-register') !== null")
