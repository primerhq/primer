"""The schema-driven provider form inside the Register overlay's frame (plan 02, Task 10; design 6.2 and 6.7).

``PC_ProviderForm`` with a ``frame`` prop hands its parts to the frame instead of laying itself out: ``frame({ body, footerLeft, footerRight, error, busy })``,
with Cancel on the left, Test (only where a draft test route exists) and the submit on the right, the save refusal as ``error`` and ``busy``
while a save or a test is out. Inside the frame the
fields are kit fields (the label alone on its line, the help under the control), the kind is a Type select of the kinds the server serves minus the
built-in ones, and an edit sends back every field it did not show. Without ``frame`` the form is the layout it always was (its pins live in
``test_provider_form*.py`` and stay as they are).

The lead's note from the #762 review: Test posted ``PC_submittable(...)`` to ``/{plural}/_test`` without ``PC_wireBody``, so a web search or web fetch Test
answered 422; it now posts the wire body, framed or not.

Everything runs for real in V8 with the real kit (``tests/ui/_register_harness.py``); the ``_types`` maps are what the route functions answer and the
artifact row is what the model serves.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import base_code, context, js, settle, types_of

FRAME = r"""
var FRAMES = [];
function frameFn(parts) {
  FRAMES.push(parts);
  return el("div", { "data-testid": "frame" },
    el("div", { "data-testid": "frame-body" }, parts.body),
    el("div", { "data-testid": "frame-left" }, parts.footerLeft),
    el("div", { "data-testid": "frame-right" }, parts.footerRight),
    parts.error ? el("div", { "data-testid": "frame-error" }, parts.error) : null);
}
var DRAFTS = [], SUBMITTED = [], CANCELLED = 0;
var ON_SUBMIT = function (body) { SUBMITTED.push(JSON.parse(JSON.stringify(body))); return Promise.resolve(); };
function FormHost(props) {
  var s = React.useState(props.value || {});
  return el(PC_ProviderForm, Object.assign({
    onChange: function (next) { DRAFTS.push(next); s[1](next); },
    onSubmit: function (body) { return ON_SUBMIT(body); },
    onCancel: function () { CANCELLED += 1; },
    frame: frameFn,
  }, props, { value: s[0] }));
}
function mountFramed(props) { MR.mount(FormHost, props); }
"""


def _served_s3_row() -> dict:
    """An s3 artifact row as the server serves it: every config field set, the two secrets masked (the model's own dump)."""
    from primer.model.providers.artifact import ArtifactStorageProvider

    row = ArtifactStorageProvider.model_validate({
        "id": "s3-archive", "provider": "s3",
        "config": {"bucket": "media", "prefix": "chat/", "endpoint_url": "http://minio:9000", "region": "eu-west-1",
                   "access_key": "AKIAEXAMPLE1234", "secret_key": "wJalrXUtnFEMI5678"},
    }).model_dump(mode="json")
    assert row["config"]["access_key"] != "AKIAEXAMPLE1234", "the served row masks its secrets"
    return row


@pytest.fixture(scope="module")
def code() -> str:
    return base_code()


@pytest.fixture
def ctx(code):
    c = context(code)
    c.eval(FRAME)
    try:
        yield c
    finally:
        c.close()


def _serve(ctx, plural: str) -> None:
    ctx.eval(f"serve(PC_typesKey({json.dumps(plural)}), {json.dumps(types_of(plural))}); MR.rerender();")


def _mount(ctx, **props) -> None:
    ctx.eval(f"mountFramed({json.dumps(props)});")


def test_a_framed_form_hands_its_footer_to_the_frame(ctx) -> None:
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, canTest=True, submitLabel="Register", editing=False)
    _serve(ctx, "web_search_providers")
    assert js(ctx, "testids('frame-left')") == ["frame-left", "provider-form-cancel"]
    right = [t for t in js(ctx, "testids('frame-right')") if t != "frame-right"]
    assert right == ["provider-form-test", "provider-form-save"], "Test, then the submit, on the right"
    assert ctx.eval("textOf(MR.find('provider-form-save'))") == "Register"
    assert "provider-form-web_search_providers" in js(ctx, "testids('frame-body')"), "the body keeps the form's own test id"
    assert ctx.eval("MR.find('frame-error') === null") is True
    ctx.eval("MR.click('provider-form-cancel')")
    assert ctx.eval("CANCELLED") == 1


def test_the_unframed_layout_is_untouched_without_a_frame(ctx) -> None:
    """No ``frame``: the form's own layout, with its own footer row and Save label (the legacy catalog mounts it until the Providers page switches over)."""
    ctx.eval("MR.mount(PC_ProviderForm, { plural: 'web_search_providers', value: { id: 'x', provider: 'tavily' }, onChange: function () {},"
             " onSubmit: function () { return Promise.resolve(); }, onCancel: function () {} })")
    _serve(ctx, "web_search_providers")
    assert ctx.eval("textOf(MR.find('provider-form-save'))") == "Save provider"
    assert ctx.eval("MR.find('pc-field:type') === null") is True, "the unframed form has no Type select"
    assert ctx.eval("FRAMES.length") == 0


def test_the_type_select_lists_served_kinds_without_built_ins(ctx) -> None:
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, canTest=True, submitLabel="Register")
    _serve(ctx, "web_search_providers")
    select = "MR.find('pc-field:type')"
    assert js(ctx, "ALL('pc-field:type').filter(function (n) { return n.type === 'option'; }).map(function (n) { return [n.props.value, textOf(n)]; })") == [
        ["tavily", "Tavily"], ["firecrawl", "Firecrawl"], ["exa", "Exa"],
    ], "DuckDuckGo is built in: never a type to create (design 6.1, B3)"
    assert ctx.eval(f"{select}.props.value") == "tavily", "the first served kind is the default, never the built-in one the map lists first"
    assert ctx.eval(f"{select}.props.disabled") is False
    row = js(ctx, "fieldRow('frame', 'pc-field:type')")
    assert row[0]["type"] == "label" and row[0]["text"] == "Type"


def test_the_excluded_kinds_are_not_offered(ctx) -> None:
    _mount(ctx, plural="web_fetch_providers", classKey="web_fetch", kindSelect=True, excludeKinds=["exa"])
    _serve(ctx, "web_fetch_providers")
    options = js(ctx, "ALL('pc-field:type').filter(function (n) { return n.type === 'option'; }).map(function (n) { return n.props.value; })")
    assert options == ["jina", "firecrawl"]


def test_changing_the_type_writes_provider_and_clears_config(ctx) -> None:
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True)
    _serve(ctx, "web_search_providers")
    ctx.eval("setValue('pc-field:id', 'search-1'); setValue('pc-field:api_key', 'tvly-1');")
    assert js(ctx, "DRAFTS[DRAFTS.length - 1]") == {"id": "search-1", "config": {"api_key": "tvly-1"}}
    ctx.eval("setValue('pc-field:type', 'exa')")
    assert js(ctx, "DRAFTS[DRAFTS.length - 1]") == {"id": "search-1", "provider": "exa", "config": {}}, "the old kind's key is dropped with its kind"
    assert ctx.eval("MR.find('pc-field:type').props.value") == "exa"


def test_the_type_select_is_locked_when_editing(ctx) -> None:
    row = {"id": "tav", "provider_type": "tavily", "provider": "tavily", "config": {"type": "tavily", "api_key": "**********"}}
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, editing=True, existingId="tav", value=row, submitLabel="Save")
    _serve(ctx, "web_search_providers")
    assert ctx.eval("MR.find('pc-field:type').props.disabled") is True
    assert ctx.eval("MR.find('pc-field:type').props.value") == "tavily"
    assert ctx.eval("MR.find('pc-field:id').props.readOnly") is True
    assert js(ctx, "fieldRow('frame', 'pc-field:id')")[2]["text"] == "Fixed after creation"


def test_a_locked_built_in_kind_still_shows_its_name(ctx) -> None:
    """Editing a built-in row (DuckDuckGo): its kind is never offered to create, but the locked select still names it."""
    row = {"id": "DuckDuckGo", "provider_type": "duckduckgo", "provider": "duckduckgo", "config": {"type": "duckduckgo"}}
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, editing=True, existingId="DuckDuckGo", value=row, readOnly=True)
    _serve(ctx, "web_search_providers")
    options = js(ctx, "ALL('pc-field:type').filter(function (n) { return n.type === 'option'; }).map(function (n) { return [n.props.value, textOf(n)]; })")
    assert options == [["duckduckgo", "DuckDuckGo"]]


@pytest.mark.parametrize("can_test", [True, False])
def test_test_is_offered_only_when_can_test(ctx, can_test: bool) -> None:
    """A Test that answers 405 is never offered (design 6.7): only a class with a draft ``_test`` route is given ``canTest``."""
    _mount(ctx, plural="artifact_storage_providers", classKey="artifact_storage", kindSelect=True, canTest=can_test)
    _serve(ctx, "artifact_storage_providers")
    assert (ctx.eval("MR.find('provider-form-test') !== null") is True) is can_test


def test_the_frame_is_told_when_a_request_is_out(ctx) -> None:
    """``busy`` is the frame's cue that a save or a test of the page's is out (the overlay shuts its type selector until it is answered)."""
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, canTest=True, submitLabel="Register")
    _serve(ctx, "web_search_providers")
    ctx.eval("setValue('pc-field:id', 'tav'); setValue('pc-field:api_key', 'k');")
    assert ctx.eval("FRAMES[FRAMES.length - 1].busy") is False
    ctx.eval("var RELEASE = null; ON_SUBMIT = function () { return new Promise(function (r) { RELEASE = r; }); };")
    ctx.eval("press('provider-form-save')")
    assert ctx.eval("FRAMES[FRAMES.length - 1].busy") is True, "a save is out"
    ctx.eval("RELEASE(); void 0;")
    settle(ctx)
    assert ctx.eval("FRAMES[FRAMES.length - 1].busy") is False
    ctx.eval("var RELEASE_TEST = null; HANDLER = function () { return new Promise(function (r) { RELEASE_TEST = r; }); };")
    ctx.eval("press('provider-form-test')")
    assert ctx.eval("FRAMES[FRAMES.length - 1].busy") is True, "a test is out"
    ctx.eval("RELEASE_TEST({ ok: true }); void 0;")
    settle(ctx)
    assert ctx.eval("FRAMES[FRAMES.length - 1].busy") is False


def test_test_posts_the_wire_body(ctx) -> None:
    """The #762 review: Test sent the form's body (``provider``) to a route that reads ``provider_type`` and ``config.type``, and answered 422."""
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, canTest=True)
    _serve(ctx, "web_search_providers")
    ctx.eval("setValue('pc-field:id', 'tav'); setValue('pc-field:api_key', 'tvly-1');")
    ctx.eval("HANDLER = function () { return Promise.resolve({ ok: true, hits: [{ title: 't', url: 'u' }] }); };")
    ctx.eval("press('provider-form-test')")
    settle(ctx)
    assert js(ctx, "CALLS") == [{
        "method": "POST", "path": "/web_search_providers/_test",
        "body": {"id": "tav", "provider_type": "tavily", "config": {"type": "tavily", "api_key": "tvly-1"}},
    }]
    result = "MR.find('provider-form-test-result')"
    assert ctx.eval(f"textOf({result})") == "Reachable"
    assert ctx.eval(f"{result}.props.role") == "status"


def test_a_failed_test_says_why(ctx) -> None:
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, canTest=True)
    _serve(ctx, "web_search_providers")
    ctx.eval("setValue('pc-field:id', 'tav'); setValue('pc-field:api_key', 'bad');")
    ctx.eval("HANDLER = function () { return Promise.resolve({ ok: false, error: 'Tavily answered 401' }); };")
    ctx.eval("press('provider-form-test')")
    settle(ctx)
    result = "MR.find('provider-form-test-result')"
    assert "Tavily answered 401" in ctx.eval(f"textOf({result})")
    assert ctx.eval(f"{result}.props.role") == "alert"


def test_the_unframed_test_posts_the_wire_body_too(ctx) -> None:
    """``runTest`` is shared: the legacy layout's Test reaches the web routes with their own names as well."""
    ctx.eval("MR.mount(PC_ProviderForm, { plural: 'web_fetch_providers', value: { id: 'jf', provider: 'jina' }, onChange: function () {},"
             " onSubmit: function () { return Promise.resolve(); } })")
    _serve(ctx, "web_fetch_providers")
    ctx.eval("press('provider-form-test')")
    settle(ctx)
    assert js(ctx, "CALLS") == [{"method": "POST", "path": "/web_fetch_providers/_test",
                                 "body": {"id": "jf", "provider_type": "jina", "config": {"type": "jina"}}}]


def test_help_sits_under_the_control_not_in_the_label(ctx) -> None:
    """Design 6.2: every label on one line, the help under its control. A secret's help says how a stored secret is kept."""
    llm = types_of("llm_providers")["openchat"]
    key_help = next(f["help"] for f in llm["config_fields"] if f["key"] == "api_key")
    _mount(ctx, plural="llm_providers", classKey="llm", kindSelect=True, editing=True, existingId="lm",
           value={"id": "lm", "provider": "openchat", "config": {"url": "http://x/v1", "api_key": "**********"}, "limits": {"max_concurrency": 2}})
    _serve(ctx, "llm_providers")
    url = js(ctx, "fieldRow('frame', 'pc-field:url')")
    assert [r["type"] for r in url] == ["label", "input"], "a field the server gives no help has none"
    assert url[0]["text"] == "Base URL *"
    secret = js(ctx, "fieldRow('frame', 'pc-field:api_key')")
    assert [r["type"] for r in secret] == ["label", "input", "div"]
    assert secret[0]["text"] == "API key (optional)", "the label is the served label and nothing else"
    assert ctx.eval("MR.find('pc-field:api_key').props.type") == "password"
    assert secret[2]["className"] == "field-help"
    assert secret[2]["text"] == key_help + " Stored secrets are shown masked; leave it as is to keep it."


def test_a_new_secret_is_not_told_about_stored_secrets(ctx) -> None:
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True)
    _serve(ctx, "web_search_providers")
    row = js(ctx, "fieldRow('frame', 'pc-field:api_key')")
    assert all("Stored secrets" not in r["text"] for r in row), "a create has no stored secret to keep"


def test_an_optional_select_shows_that_it_is_not_set(ctx) -> None:
    """``flavor`` is optional and the model defaults it; a select that showed its first option while sending nothing would show a value it never sends."""
    _mount(ctx, plural="llm_providers", classKey="llm", kindSelect=True)
    _serve(ctx, "llm_providers")
    options = js(ctx, "ALL('pc-field:flavor').filter(function (n) { return n.type === 'option'; }).map(function (n) { return [n.props.value, textOf(n)]; })")
    assert options[0] == ["", "Not set"]
    assert ["lmstudio", "LM Studio"] in options, "the flavors read with their names"
    assert ctx.eval("MR.find('pc-field:flavor').props.value") == ""


def test_a_built_in_with_nothing_to_configure_offers_no_submit(ctx) -> None:
    row = {"id": "local", "provider_type": "local", "provider": "local", "config": {"type": "local"}}
    _mount(ctx, plural="web_fetch_providers", classKey="web_fetch", kindSelect=True, editing=True, existingId="local", value=row, readOnly=True,
           submitLabel="Save", canTest=True)
    _serve(ctx, "web_fetch_providers")
    assert ctx.eval("MR.find('provider-form-save') === null") is True
    assert "Built in; nothing to configure." in ctx.eval("textOf(MR.find('frame-body'))")
    assert ctx.eval("MR.find('provider-form-cancel') !== null") is True


def test_the_framed_submit_waits_for_a_valid_id(ctx) -> None:
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, submitLabel="Register")
    _serve(ctx, "web_search_providers")
    assert ctx.eval("MR.find('provider-form-save').props.disabled") is True, "no id yet"
    ctx.eval("setValue('pc-field:id', 'my/search'); setValue('pc-field:api_key', 'k');")
    assert ctx.eval("MR.find('provider-form-save').props.disabled") is True
    row = js(ctx, "fieldRow('frame', 'pc-field:id')")
    assert row[-1]["role"] == "alert" and "Start with a letter or digit" in row[-1]["text"]
    ctx.eval("setValue('pc-field:id', 'my-search')")
    assert ctx.eval("MR.find('provider-form-save').props.disabled") is False


def test_a_refused_save_is_the_frames_error_and_save_works_again(ctx) -> None:
    envelope = {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": "id 'tav' already exists", "instance": "/v1/web_search_providers"}
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, submitLabel="Register")
    _serve(ctx, "web_search_providers")
    ctx.eval("setValue('pc-field:id', 'tav'); setValue('pc-field:api_key', 'k');")
    ctx.eval(f"ON_SUBMIT = function () {{ return refuse({json.dumps(envelope)}); }};")
    ctx.eval("press('provider-form-save')")
    assert settle(ctx) == "fulfilled", "the click handler's promise never rejects"
    assert "already exists" in ctx.eval("FRAMES[FRAMES.length - 1].error")
    save = "nativeButton('provider-form-save').props"
    assert ctx.eval(f"{save}['aria-busy'] === undefined && !{save}.disabled") is True


def test_the_framed_save_stays_focusable_while_its_request_is_out(ctx) -> None:
    """``<Btn busy>``, not ``disabled`` (tests/ui/test_busy_buttons_ratchet.py): a focused button that turns disabled drops the focus out of the dialog."""
    _mount(ctx, plural="web_search_providers", classKey="web_search", kindSelect=True, canTest=True, submitLabel="Register")
    _serve(ctx, "web_search_providers")
    ctx.eval("setValue('pc-field:id', 'tav'); setValue('pc-field:api_key', 'k');")
    ctx.eval("var RELEASE = null; ON_SUBMIT = function (b) { SUBMITTED.push(b); return new Promise(function (r) { RELEASE = r; }); };")
    ctx.eval("press('provider-form-save')")
    save = "nativeButton('provider-form-save').props"
    assert ctx.eval(f"{save}['aria-busy']") == "true" and ctx.eval(f"!!{save}.disabled") is False
    assert ctx.eval("nativeButton('provider-form-test').props['aria-disabled']") == "true", "Test is held off while the save is out"
    ctx.eval(f"{save}.onClick({{ preventDefault: function () {{}}, stopPropagation: function () {{}} }}); void 0;")
    ctx.eval("MR.find('provider-form-save').props.onClick({ preventDefault: function () {}, stopPropagation: function () {} }); void 0;")
    assert ctx.eval("SUBMITTED.length") == 1, "a second press sent a second request (the button refuses it, and so does the form)"
    ctx.eval("RELEASE(); void 0;")
    settle(ctx)
    assert ctx.eval(f"{save}['aria-busy'] === undefined") is True


def test_the_framed_limits_show_only_what_the_class_reads(ctx) -> None:
    """A wizard class whose own pages are not loaded falls back to this form: its limits box is today's three fields, cut to what the class's adapters read
    (the Hugging Face embedder reads max_concurrency only)."""
    _mount(ctx, plural="embedding_providers", classKey="embedding", kindSelect=True, value={"provider": "huggingface"})
    _serve(ctx, "embedding_providers")
    shown = js(ctx, "MR.findAll('pc-limit:').map(function (n) { return n.props['data-testid']; })")
    assert shown == ["pc-limit:max_concurrency"]
    ctx.eval("setValue('pc-field:type', 'openai')")
    shown = js(ctx, "MR.findAll('pc-limit:').map(function (n) { return n.props['data-testid']; })")
    assert shown == ["pc-limit:max_concurrency"], "connect timeout sits in the fold"
    ctx.eval("MR.click('pc-limits-more:toggle')")
    shown = js(ctx, "MR.findAll('pc-limit:').map(function (n) { return n.props['data-testid']; })")
    assert shown == ["pc-limit:max_concurrency", "pc-limit:connect_timeout_seconds"]


def test_a_limits_problem_holds_the_submit(ctx) -> None:
    _mount(ctx, plural="llm_providers", classKey="llm", kindSelect=True, value={"provider": "anthropic", "id": "a1"})
    _serve(ctx, "llm_providers")
    assert ctx.eval("MR.find('provider-form-save').props.disabled") is False
    ctx.eval("setValue('pc-limit:max_concurrency', '0')")
    assert ctx.eval("MR.find('provider-form-save').props.disabled") is True
    assert js(ctx, "fieldRow('frame', 'pc-limit:max_concurrency')")[-1]["text"].endswith("Enter a whole number of 1 or more")


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    """Master Review Focus 1: an edit sends back every field it did not show (or did not change) unchanged, the masked secrets included (the server restores
    a secret it served masked), plus one key a later server may add at each level."""
    row = _served_s3_row()
    row["labels"] = {"team": "media"}
    row["config"]["storage_class"] = "STANDARD_IA"
    draft = dict(row, config=dict(row["config"]))
    _mount(ctx, plural="artifact_storage_providers", classKey="artifact_storage", kindSelect=True, editing=True, existingId=row["id"], value=draft,
           submitLabel="Save")
    _serve(ctx, "artifact_storage_providers")
    ctx.eval("setValue('pc-field:region', 'us-east-2')")
    ctx.eval("press('provider-form-save')")
    assert settle(ctx) == "fulfilled"
    (body,) = js(ctx, "SUBMITTED")
    expected = dict(row, config=dict(row["config"], region="us-east-2"))
    assert body == expected


def test_invalidate_is_exported_for_the_overlay(ctx) -> None:
    assert ctx.eval("typeof window.PC_InvalidateAction") == "function"
