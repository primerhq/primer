"""The Register provider overlay, ``window.PC_RegisterOverlay`` (plan 02, Task 11; seam I-3 with ``excludeKinds`` and ``onDeleted``; design 6.1, 6.8, 6.9).

One overlay registers and edits every provider class. It is the kit's ``PK_FormOverlay``: the title, the type selector at the centre top ("Register a
[icon] <Type> provider", the classes in the sidebar order, Web Fetch only when a caller allows it), the page of the chosen class below it, Cancel on the
left and the page's action on the right. A page renders INTO the overlay through ``frame({ body, footerLeft, footerRight, error })``: a wizard class's
own page from ``PC_REGISTER_PAGES`` (Tasks 12 to 15; stubbed here), a form class the schema form, a panel class its own create form. Changing the type
remounts the page (typed values are dropped); edit mode locks the type, titles "Edit <id>", offers Delete behind a confirm (never on a built-in row) and
Invalidate for a class that caches adapters; a save toasts and closes.

Everything runs in V8 with the real kit, form and foundation (``tests/ui/_register_harness.py``); the ``_types`` maps are what the route functions answer
and the refusals are what the real routers answer.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import UI, base_code, context, js, settle, types_of

STUBS = r"""
var PAGE_PROPS = [], PANEL_PROPS = [], SAVED = [], DELETED = [], CLOSED = 0;
var STUB_ERROR = null;
function StubPage(props) {
  PAGE_PROPS.push(props);
  var s = React.useState("");
  return props.frame({
    body: el("div", { "data-testid": "stub-body" },
      el("input", { "data-testid": "stub-input", value: s[0], onChange: function (e) { s[1](e.target.value); } })),
    footerLeft: el("button", { "data-testid": "stub-cancel", onClick: props.onClose }, "Cancel"),
    footerRight: el("button", { "data-testid": "stub-next", onClick: function () { props.onSaved({ id: "made-1" }); } }, "Next"),
    error: STUB_ERROR,
  });
}
["llm", "embedding", "cross_encoder", "tts", "stt"].forEach(function (k) { window.PC_REGISTER_PAGES[k] = StubPage; });
function StubPanel(props) {
  PANEL_PROPS.push(props);
  return props.frame({
    body: el("div", { "data-testid": "panel-body" }, props.existing ? props.existing.id : "new"),
    footerLeft: el("button", { "data-testid": "panel-cancel", onClick: props.onClose }, "Cancel"),
    footerRight: el("button", { "data-testid": "panel-save", onClick: function () { props.onSaved({ id: "panel-1" }); } }, "Register"),
  });
}
window.SSPCreateModal = StubPanel;
window.WorkspaceProviderCreateModal = StubPanel;
window.NewChannelProviderModal = StubPanel;
// A caller mounts the overlay afresh each time it opens it; the harness keeps a component's state while the same type stays at the root, so the root is
// emptied first.
function mountOverlay(props, withDeleted) {
  var base = { onClose: function () { CLOSED += 1; }, onSaved: function (row) { SAVED.push(row); } };
  if (withDeleted) base.onDeleted = function (id) { DELETED.push(id); };
  MR.mount(function Closed() { return null; }, {});
  MR.mount(PC_RegisterOverlay, Object.assign(base, props));
}
function options(testid) {
  return ALL(testid).filter(function (n) { return n.type === "option"; }).map(function (n) { return n.props.value; });
}
function bodyOf() { return ALL("pc-register").filter(function (n) { return n.props && n.props.className === "modal-b"; })[0]; }
"""

LABELS = [
    "LLM provider", "Embedding provider", "Semantic Search Storage provider", "Cross Encoding provider", "Workspace provider",
    "Web Search provider", "Channel provider", "Artifact Store provider", "TTS provider", "ASR provider",
]


@pytest.fixture(scope="module")
def code() -> str:
    return base_code(with_overlay=True)


@pytest.fixture
def ctx(code):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def _serve(ctx, plural: str) -> None:
    ctx.eval(f"serve(PC_typesKey({json.dumps(plural)}), {json.dumps(types_of(plural))}); MR.rerender();")


def _mount(ctx, with_deleted: bool = False, **props) -> None:
    ctx.eval(f"mountOverlay({json.dumps(props)}, {json.dumps(with_deleted)});")


def _delete_403() -> dict:
    """What a reserved-row DELETE answers: the real router and the real error handlers (``primer/api/routers/providers.py``)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.errors import register_error_handlers
    from primer.api.routers import providers

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(providers.embedding_provider_router, prefix="/v1")
    app.dependency_overrides[providers.get_embedding_provider_storage] = lambda: object()
    answer = TestClient(app, raise_server_exceptions=False).delete("/v1/embedding_providers/huggingface")
    assert answer.status_code == 403, answer.text
    return answer.json()


# ---- the shell ---------------------------------------------------------------------------------------------------------------------------------------


def test_the_overlay_is_the_kit_overlay(ctx) -> None:
    _mount(ctx, initialClass="llm")
    dialog = "MR.find('pc-register').props"
    assert ctx.eval(f"{dialog}.className").split() == ["modal", "pk-overlay"]
    assert ctx.eval(f"{dialog}.role") == "dialog"
    assert "Register provider" in js(ctx, "MR.texts()")
    assert "Register a" in js(ctx, "MR.texts()")
    assert ctx.eval("textOf(MR.find('pc-register-type'))") == "LLM provider▾"
    assert ctx.eval("MR.find('stub-body') !== null") is True, "the page's body is in the overlay"
    assert ctx.eval("bodyOf() !== undefined") is True


def test_start_class_rules(ctx) -> None:
    cases = [
        (("tts", None, None), "tts"),
        (("all", None, None), "llm"),
        (("web_fetch", None, None), "llm"),
        (("nope", None, None), "llm"),
        ((None, None, None), "llm"),
        (("llm", ["web_fetch"], None), "web_fetch"),
        (("web_fetch", ["web_search", "web_fetch"], None), "web_fetch"),
        (("tts", [], None), "tts"),
        (("web_fetch", None, {"id": "jf"}), "web_fetch"),
        (("web_fetch", ["llm"], {"id": "jf"}), "web_fetch"),
    ]
    for args, expected in cases:
        assert ctx.eval(f"PC_startClass.apply(null, {json.dumps(list(args))})") == expected, args


def test_the_selector_lists_ten_types_in_sidebar_order(ctx) -> None:
    assert [o["label"] for o in js(ctx, "PC_selectorOptions()")] == LABELS
    assert js(ctx, "PC_selectorOptions().map(function (o) { return o.iconPath; })") == js(
        ctx, "PC_CLASSES.filter(function (c) { return c.nav; }).map(function (c) { return c.iconPath; })")
    _mount(ctx, initialClass="all")
    ctx.eval("MR.click('pc-register-type')")
    items = js(ctx, "ALL('pc-register-type:menu').filter(function (n) { return n.props.role === 'menuitemradio'; }).map(function (n) { return textOf(n); })")
    assert items == LABELS, "LLM first, ASR last, no Web Fetch"
    assert ctx.eval("MR.find('pc-register-type:llm').props['aria-checked']") == "true", "opened from All, it starts on LLM"


def test_allowed_classes_restrict_and_lock_the_selector(ctx) -> None:
    _mount(ctx, initialClass="web_fetch", allowedClasses=["web_fetch"])
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled']") == "true", "one option: shown, not changeable"
    assert ctx.eval("textOf(MR.find('pc-register-type'))") == "Web Fetch provider"
    ctx.eval("MR.click('pc-register-type')")
    assert ctx.eval("MR.find('pc-register-type:menu') === null") is True
    assert [o["value"] for o in js(ctx, "PC_selectorOptions(['web_search', 'web_fetch'])")] == ["web_search", "web_fetch"]
    _mount(ctx, initialClass="llm", allowedClasses=["web_search", "web_fetch"])
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled'] === undefined") is True
    assert ctx.eval("textOf(MR.find('pc-register-type'))") == "Web Search provider▾", "a class the caller does not allow is never shown"


def test_exclude_kinds_reach_the_kind_select(ctx) -> None:
    _mount(ctx, initialClass="web_fetch", allowedClasses=["web_fetch"], excludeKinds=["local"])
    _serve(ctx, "web_fetch_providers")
    assert js(ctx, "options('pc-field:type')") == ["jina", "firecrawl", "exa"]
    _mount(ctx, initialClass="web_fetch", allowedClasses=["web_fetch"], excludeKinds=["exa"])
    assert js(ctx, "options('pc-field:type')") == ["jina", "firecrawl"]


def test_changing_the_type_drops_typed_values(ctx) -> None:
    _mount(ctx, initialClass="llm")
    ctx.eval("setValue('stub-input', 'typed for llm')")
    assert ctx.eval("MR.find('stub-input').props.value") == "typed for llm"
    ctx.eval("MR.click('pc-register-type'); MR.click('pc-register-type:embedding');")
    assert ctx.eval("textOf(MR.find('pc-register-type'))") == "Embedding provider▾"
    assert ctx.eval("MR.find('stub-input').props.value") == ""
    assert ctx.eval("PAGE_PROPS[PAGE_PROPS.length - 1].klass.key") == "embedding"
    ctx.eval("MR.click('pc-register-type'); MR.click('pc-register-type:llm');")
    assert ctx.eval("MR.find('stub-input').props.value") == "", "going back does not bring the old values back"


def test_a_wizard_page_gets_the_page_contract(ctx) -> None:
    _mount(ctx, initialClass="tts", excludeKinds=["x"])
    props = "PAGE_PROPS[PAGE_PROPS.length - 1]"
    assert ctx.eval(f"{props}.klass === PC_classByKey('tts')") is True
    assert ctx.eval(f"{props}.editRow") is None
    assert js(ctx, f"{props}.excludeKinds") == ["x"]
    assert js(ctx, f"[typeof {props}.frame, typeof {props}.onSaved, typeof {props}.onClose]") == ["function", "function", "function"]
    ctx.eval("MR.click('stub-cancel')")
    assert ctx.eval("CLOSED") == 1


def test_a_page_save_toasts_hands_back_the_row_and_closes(ctx) -> None:
    _mount(ctx, initialClass="stt")
    ctx.eval("MR.click('stub-next')")
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "made-1"}]
    assert js(ctx, "SAVED") == [{"id": "made-1"}]
    assert ctx.eval("CLOSED") == 1


def test_the_page_error_is_the_last_body_line(ctx) -> None:
    ctx.eval("STUB_ERROR = 'The provider answered 401';")
    _mount(ctx, initialClass="llm")
    err = "MR.find('pc-register-error')"
    assert ctx.eval(f"{err}.props.role") == "alert"
    assert ctx.eval(f"textOf({err})") == "The provider answered 401"
    kids = js(ctx, "__flat(bodyOf().children).map(function (n) { return n.props['data-testid'] || n.props.className || n.type; })")
    assert kids[-1] == "pc-register-error", kids


def test_a_wizard_class_without_its_page_uses_the_schema_form(ctx) -> None:
    """Until a wizard class's own pages are loaded (Tasks 12 to 15), the overlay still registers it, through the framed schema form."""
    ctx.eval("delete window.PC_REGISTER_PAGES.llm;")
    _mount(ctx, initialClass="llm")
    _serve(ctx, "llm_providers")
    assert ctx.eval("MR.find('provider-form-llm_providers') !== null") is True
    assert js(ctx, "options('pc-field:type')")[0] == "openchat"
    assert ctx.eval("textOf(MR.find('provider-form-save'))") == "Register"


# ---- form classes ------------------------------------------------------------------------------------------------------------------------------------


def test_a_web_search_registers_with_the_wire_body(ctx) -> None:
    _mount(ctx, initialClass="web_search")
    _serve(ctx, "web_search_providers")
    assert js(ctx, "options('pc-field:type')") == ["tavily", "firecrawl", "exa"]
    ctx.eval("setValue('pc-field:id', 'tav'); setValue('pc-field:api_key', 'tvly-1');")
    assert ctx.eval("MR.find('provider-form-test') !== null") is True, "web search has a draft _test route"
    ctx.eval("press('provider-form-save')")
    assert settle(ctx) == "fulfilled"
    assert js(ctx, "CALLS") == [{"method": "POST", "path": "/web_search_providers",
                                 "body": {"id": "tav", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "tvly-1"}}}]
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "tav"}]
    assert js(ctx, "SAVED")[0]["id"] == "tav"
    assert ctx.eval("CLOSED") == 1


def test_artifact_storage_offers_no_test_until_its_route_exists(ctx) -> None:
    """A Test that answers 405 is never offered (design 6.7): the class has no draft ``_test`` route yet (Plan 09 adds it)."""
    _mount(ctx, initialClass="artifact_storage")
    _serve(ctx, "artifact_storage_providers")
    assert ctx.eval("MR.find('provider-form-test') === null") is True
    assert js(ctx, "options('pc-field:type')") == ["db", "filesystem", "s3"]


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    """Master Review Focus 1, through the overlay's PUT: the served row, a field changed, every other field (the masked secrets and keys a later server may
    add included) sent back unchanged."""
    from primer.model.providers.artifact import ArtifactStorageProvider

    row = ArtifactStorageProvider.model_validate({
        "id": "s3-archive", "provider": "s3",
        "config": {"bucket": "media", "prefix": "chat/", "endpoint_url": "http://minio:9000", "region": "eu-west-1",
                   "access_key": "AKIAEXAMPLE1234", "secret_key": "wJalrXUtnFEMI5678"},
    }).model_dump(mode="json")
    row["labels"] = {"team": "media"}
    row["config"]["storage_class"] = "STANDARD_IA"
    _mount(ctx, initialClass="artifact_storage", editRow=row)
    _serve(ctx, "artifact_storage_providers")
    assert ctx.eval("textOf(MR.find('provider-form-save'))") == "Save"
    ctx.eval("setValue('pc-field:region', 'us-east-2')")
    ctx.eval("press('provider-form-save')")
    assert settle(ctx) == "fulfilled"
    (call,) = js(ctx, "CALLS")
    assert call["method"] == "PUT" and call["path"] == "/artifact_storage_providers/s3-archive"
    assert call["body"] == dict(row, config=dict(row["config"], region="us-east-2"))
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider saved", "detail": "s3-archive"}]
    assert ctx.eval("CLOSED") == 1


def test_an_edit_of_a_web_row_puts_its_own_names(ctx) -> None:
    row = {"id": "tav", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "**********"}}
    _mount(ctx, initialClass="web_search", editRow=row)
    _serve(ctx, "web_search_providers")
    assert ctx.eval("MR.find('pc-field:type').props.value") == "tavily", "the served provider_type seeds the kind"
    ctx.eval("press('provider-form-save')")
    settle(ctx)
    assert js(ctx, "CALLS") == [{"method": "PUT", "path": "/web_search_providers/tav", "body": row}]


def test_a_read_only_built_in_offers_no_save(ctx) -> None:
    _mount(ctx, initialClass="web_fetch", allowedClasses=["web_fetch"], editRow={"id": "local", "provider_type": "local", "config": {"type": "local"}})
    _serve(ctx, "web_fetch_providers")
    assert ctx.eval("MR.find('provider-form-save') === null") is True
    assert "Built in; nothing to configure." in ctx.eval("textOf(MR.find('pc-register'))")
    assert ctx.eval("MR.find('pc-register-delete') === null") is True


# ---- edit and delete ---------------------------------------------------------------------------------------------------------------------------------


def test_edit_mode_locks_the_type_and_titles_edit(ctx) -> None:
    _mount(ctx, initialClass="cross_encoder", editRow={"id": "ce-1", "provider": "huggingface"})
    assert "Edit ce-1" in js(ctx, "MR.texts()")
    assert "Register provider" not in js(ctx, "MR.texts()")
    assert "Register a" not in js(ctx, "MR.texts()"), "an edit registers nothing"
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled']") == "true"
    assert ctx.eval("textOf(MR.find('pc-register-type'))") == "Cross Encoding provider"
    assert ctx.eval("PAGE_PROPS[PAGE_PROPS.length - 1].editRow.id") == "ce-1"


def test_a_hidden_class_is_named_in_edit_mode(ctx) -> None:
    """Web Fetch has no selector entry of its own, but an edit of a fetcher (Plan 04's Open) still names it."""
    _mount(ctx, initialClass="web_fetch", editRow={"id": "jf", "provider_type": "jina", "config": {"type": "jina"}})
    assert ctx.eval("textOf(MR.find('pc-register-type'))") == "Web Fetch provider"


def test_delete_confirms_then_deletes_and_reports(ctx) -> None:
    _mount(ctx, with_deleted=True, initialClass="web_search", editRow={"id": "tav", "provider_type": "tavily", "config": {"type": "tavily"}})
    left = js(ctx, "testids('pc-register')")
    assert left.index("pc-register-delete") > left.index("provider-form-cancel"), "Delete sits after the page's own left footer"
    ctx.eval("CONFIRM_ANSWER = false; press('pc-register-delete');")
    settle(ctx)
    assert js(ctx, "CALLS") == [], "a declined confirm deletes nothing"
    ctx.eval("CONFIRM_ANSWER = true; press('pc-register-delete');")
    settle(ctx)
    assert js(ctx, "CONFIRMS")[-1] == {
        "title": "Delete tav?",
        "message": "This removes the Web Search provider. Anything that uses it stops working.",
        "confirmLabel": "Delete provider",
        "danger": True,
    }
    assert js(ctx, "CALLS") == [{"method": "DELETE", "path": "/web_search_providers/tav", "body": None}]
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider deleted", "detail": "tav"}]
    assert js(ctx, "DELETED") == ["tav"]
    assert ctx.eval("CLOSED") == 0, "with onDeleted, the caller decides what follows"


def test_without_on_deleted_a_delete_closes_the_overlay(ctx) -> None:
    _mount(ctx, initialClass="llm", editRow={"id": "lm/odd id", "provider": "openchat"})
    ctx.eval("press('pc-register-delete')")
    settle(ctx)
    assert js(ctx, "CALLS") == [{"method": "DELETE", "path": "/llm_providers/lm%2Fodd%20id", "body": None}], "the id is one path segment"
    assert ctx.eval("CLOSED") == 1


def test_a_refused_delete_stays_open_with_the_reason(ctx) -> None:
    envelope = _delete_403()
    _mount(ctx, with_deleted=True, initialClass="embedding", editRow={"id": "emb-1", "provider": "openai"})
    ctx.eval(f"HANDLER = function (m) {{ return m === 'DELETE' ? refuse({json.dumps(envelope)}) : Promise.resolve({{}}); }};")
    ctx.eval("press('pc-register-delete')")
    assert settle(ctx) == "fulfilled", "the click handler's promise never rejects"
    err = "MR.find('pc-register-error')"
    assert ctx.eval(f"{err}.props.role") == "alert"
    assert ctx.eval(f"textOf({err})") == ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelope)})).message")
    assert "cannot be deleted" in ctx.eval(f"textOf({err})")
    assert ctx.eval("CLOSED") == 0 and js(ctx, "DELETED") == [] and js(ctx, "TOASTS") == []
    assert ctx.eval("nativeButton('pc-register-delete').props['aria-busy'] === undefined") is True, "Delete works again"


def test_the_delete_button_stays_focusable_while_its_request_is_out(ctx) -> None:
    _mount(ctx, with_deleted=True, initialClass="llm", editRow={"id": "lm", "provider": "openchat"})
    ctx.eval("var RELEASE = null; HANDLER = function () { return new Promise(function (r) { RELEASE = r; }); };")
    ctx.eval("press('pc-register-delete')")
    settle(ctx)
    button = "nativeButton('pc-register-delete').props"
    assert ctx.eval(f"{button}['aria-busy']") == "true" and ctx.eval(f"!!{button}.disabled") is False


def test_built_ins_offer_no_delete(ctx) -> None:
    built_ins = js(ctx, "PC_BUILTIN_IDS")
    assert built_ins
    for class_key, ids in built_ins.items():
        for rid in ids:
            _mount(ctx, initialClass=class_key, editRow={"id": rid, "provider": "x", "provider_type": "x"})
            assert ctx.eval("MR.find('pc-register-delete') === null") is True, (class_key, rid)
    _mount(ctx, initialClass="web_search", editRow={"id": "tav", "provider_type": "tavily"})
    assert ctx.eval("MR.find('pc-register-delete') !== null") is True, "a row the operator made can be deleted"


def test_invalidate_is_offered_when_editing_a_class_that_caches_adapters(ctx) -> None:
    _mount(ctx, initialClass="embedding", editRow={"id": "emb-1", "provider": "openai"})
    assert ctx.eval("MR.find('pc-register-invalidate') !== null") is True
    ctx.eval("press('provider-form-invalidate-action')")
    settle(ctx)
    assert js(ctx, "CALLS") == [{"method": "POST", "path": "/embedding_providers/emb-1/invalidate", "body": None}]
    _mount(ctx, initialClass="embedding")
    assert ctx.eval("MR.find('pc-register-invalidate') === null") is True, "nothing to invalidate before a save"
    _mount(ctx, initialClass="web_search", editRow={"id": "tav", "provider_type": "tavily"})
    assert ctx.eval("MR.find('pc-register-invalidate') === null") is True


# ---- panel classes -----------------------------------------------------------------------------------------------------------------------------------


def test_panel_forms_render_inside_the_frame(ctx) -> None:
    assert ctx.eval("PC_PANEL_FORMS.ssp() === window.SSPCreateModal") is True
    assert ctx.eval("PC_PANEL_FORMS.workspace() === window.WorkspaceProviderCreateModal") is True
    assert ctx.eval("PC_PANEL_FORMS.channel() === window.NewChannelProviderModal") is True
    _mount(ctx, initialClass="ssp")
    props = "PANEL_PROPS[PANEL_PROPS.length - 1]"
    assert ctx.eval(f"{props}.existing") is None
    assert js(ctx, f"[typeof {props}.frame, typeof {props}.onSaved, typeof {props}.onClose, typeof {props}.pushToast]") == [
        "function", "function", "function", "function"]
    assert ctx.eval("MR.find('pc-register').props.className").split() == ["modal", "pk-overlay"]
    assert ctx.eval("textOf(MR.find('panel-body'))") == "new"
    ctx.eval("MR.click('panel-save')")
    assert js(ctx, "SAVED") == [{"id": "panel-1"}]
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "panel-1"}]
    assert ctx.eval("CLOSED") == 1
    _mount(ctx, initialClass="channel", editRow={"id": "slack-1", "provider": "slack"})
    assert ctx.eval(f"{props}.existing.id") == "slack-1"
    assert ctx.eval("MR.find('pc-register-delete') !== null") is True


# ---- requests and loading ----------------------------------------------------------------------------------------------------------------------------

REQUEST_HOST = r"""
var TAKEN = [];
function RequestHost(props) {
  PC_useRegisterRequest(function (classKey) { TAKEN.push([props.name, classKey]); });
  return el("div", { "data-testid": "req-" + props.name });
}
function Empty() { return el("div", null); }
"""


def test_a_pending_register_request_is_taken_once(ctx) -> None:
    """The palette verb asks for the overlay before the Providers page has mounted: the request waits for the first page that takes it, and is taken once."""
    ctx.eval(REQUEST_HOST)
    ctx.eval("PC_requestRegister('tts')")
    ctx.eval("MR.mount(RequestHost, { name: 'first' })")
    assert js(ctx, "TAKEN") == [["first", "tts"]]
    ctx.eval("MR.mount(Empty, {}); MR.mount(RequestHost, { name: 'second' });")
    assert js(ctx, "TAKEN") == [["first", "tts"]], "a request is taken once"
    ctx.eval("PC_requestRegister('llm')")
    assert js(ctx, "TAKEN") == [["first", "tts"], ["second", "llm"]], "a mounted page takes a request at once"
    ctx.eval("MR.mount(Empty, {}); PC_requestRegister(null); MR.mount(RequestHost, { name: 'third' });")
    assert js(ctx, "TAKEN")[-1] == ["third", None], "a request for no class in particular is a request too"


def test_index_loads_the_overlay_after_register_common() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    tag = 'src="components/providers/register-overlay.jsx"'
    assert tag in html
    assert html.index(tag) > html.index('src="components/providers/register-common.jsx"')
    assert html.index(tag) > html.index('src="components/shared/platform-kit.jsx"')
    assert html.index(tag) < html.index('src="components/provider-catalog.jsx"')


def test_the_overlay_is_a_sheet_on_a_phone(ctx) -> None:
    ctx.eval("MOBILE = true;")
    _mount(ctx, initialClass="llm")
    assert ctx.eval("MR.find('pc-register').props.className").split() == ["sheet", "pk-overlay"]
    assert ctx.eval("MR.find('pc-register-type') !== null") is True
    assert ctx.eval("MR.find('stub-next') !== null") is True
