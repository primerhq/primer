"""The pieces the wizard pages of the Register provider overlay share (plan 02, Tasks 14 and 15; ``ui/components/providers/register-common.jsx``).

The Embedding, Cross Encoding, TTS and ASR pages are each two pages in one: the connection, then a list to choose from. What they share lives in ``register-common.jsx``
and is pinned here, with the real kit, form and foundation in V8 (``tests/ui/_register_harness.py``): the state of a request that is out and of the step
(``PC_useWizard``), the hand-off of the focus after a page change (``PC_PageFocus``), one served config field as a kit field (``PC_ServedField``), the lines that say the fields
of a kind are loading or could not be loaded (``PC_TypesNotice``), the words of a count, and the row a page tells the caller about after a save (``PC_savedRow``).
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import base_code, context, js

STUBS = r"""
var W = null, RENDERS = 0;
function Probe(props) {
  RENDERS += 1;
  W = PC_useWizard(props.plural || "llm_providers", !!props.blocked);
  return el("div", { "data-testid": "probe" });
}
function mountProbe(props) { MR.mount(Probe, props || {}); }
function serveTypes(plural) { serve(PC_typesKey(plural), TYPES[plural]); MR.rerender(); }
var TYPES = {};
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code()


@pytest.fixture
def ctx(code):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def _refusal(status: int = 500, detail: str = "the database went away") -> dict:
    return {"type": "/errors/internal", "title": "Internal Server Error", "status": status, "detail": detail, "instance": "/v1/x"}


# ---- the small pure helpers --------------------------------------------------------------------------------------------------------------------------


def test_count_of_says_one_in_the_singular(ctx) -> None:
    assert js(ctx, "[PC_countOf(0, 'model', 'models'), PC_countOf(1, 'model', 'models'), PC_countOf(3, 'voice', 'voices')]") == ["0 models", "1 model", "3 voices"]


def test_is_blank_reads_nothing_and_spaces_as_blank(ctx) -> None:
    assert js(ctx, "[PC_isBlank(undefined), PC_isBlank(null), PC_isBlank(''), PC_isBlank('  \\n'), PC_isBlank(0), PC_isBlank('a'), PC_isBlank(false)]") == [
        True, True, True, True, False, False, False]


def test_saved_row_is_the_answer_and_never_the_request(ctx) -> None:
    """The request body holds every secret as it was typed; the caller (a list that reloads, a parent that keeps the row) must never be handed it."""
    row = {"id": "p", "config": {"api_key": "**********1234"}}
    assert js(ctx, f"PC_savedRow({json.dumps(row)}, {{ id: 'p', config: {{ api_key: 'sk-typed-in-the-clear' }} }})") == row
    for answer in ("null", "undefined", "'ok'", "7"):
        got = js(ctx, f"PC_savedRow({answer}, {{ id: 'p', config: {{ api_key: 'sk-typed-in-the-clear' }} }})")
        assert got == {"id": "p"}, answer
        assert "sk-typed-in-the-clear" not in json.dumps(got)
    assert js(ctx, "PC_savedRow(null, {})") == {} and js(ctx, "PC_savedRow(null, null)") == {}


def test_a_new_providers_config_starts_on_the_models_default_flavor(ctx) -> None:
    """The Flavor box shows a value, so the same value is sent: ``other`` (the model's own default) when the kind offers it, else the first option, and nothing for a kind
    with no flavor."""
    from tests.ui._register_harness import types_of

    served = types_of("embedding_providers")
    ctx.eval(f"var FIELDS = {json.dumps(served['openai']['config_fields'])}, HF = {json.dumps(served['huggingface']['config_fields'])};")
    assert js(ctx, "PC_startConfig(FIELDS)") == {"flavor": "other"} and js(ctx, "PC_startConfig(HF)") == {} and js(ctx, "PC_startConfig(null)") == {}
    assert js(ctx, "PC_startConfig([{ key: 'flavor', type: 'enum', options: ['a', 'b'] }])") == {"flavor": "a"}


def test_a_new_providers_draft_is_filled_in_once_and_an_edit_is_never_filled_in(ctx) -> None:
    from tests.ui._register_harness import types_of

    served = types_of("embedding_providers")
    ctx.eval(f"var FIELDS = {json.dumps(served['openai']['config_fields'])};")
    filled = js(ctx, "PC_fillDraft({ id: 'e', config: { url: 'http://x/v1' } }, 'openai', FIELDS, ['max_concurrency', 'connect_timeout_seconds'])")
    assert filled == {"id": "e", "provider": "openai", "config": {"flavor": "other", "url": "http://x/v1"}, "limits": {"max_concurrency": 1}}
    kept = js(ctx, "PC_fillDraft({ id: 'e', config: { flavor: 'vllm' }, limits: { max_concurrency: 4 } }, 'openai', FIELDS, ['max_concurrency'])")
    assert kept["config"] == {"flavor": "vllm"} and kept["limits"] == {"max_concurrency": 4}, "what was typed wins over the start"


def test_the_helpers_are_exported_on_window(ctx) -> None:
    for name in ("PC_useWizard", "PC_PageFocus", "PC_ServedField", "PC_TypesNotice", "PC_countOf", "PC_isBlank", "PC_savedRow", "PC_PageHead", "PC_startConfig", "PC_fillDraft"):
        assert ctx.eval(f"typeof window.{name}") == "function", name


# ---- the state of a wizard page ----------------------------------------------------------------------------------------------------------------------


def test_a_request_out_refuses_the_next_one_until_it_ends(ctx) -> None:
    ctx.eval("mountProbe();")
    assert ctx.eval("W.begin('discover')") is True
    ctx.eval("MR.rerender();")
    assert ctx.eval("W.out") == "discover", "the state says which request is out"
    assert ctx.eval("W.begin('register')") is False, "one request of the page's own at a time"
    ctx.eval("W.end(); MR.rerender();")
    assert ctx.eval("W.out") is None and ctx.eval("W.begin('register')") is True


def test_the_overlays_own_request_holds_every_request_of_the_page_off(ctx) -> None:
    """``blocked`` is the overlay's Delete being out: a page request must not run beside it."""
    ctx.eval("mountProbe({ blocked: true });")
    assert ctx.eval("W.begin('discover')") is False and ctx.eval("W.out") is None
    ctx.eval("MR.rerender({ blocked: false });")
    assert ctx.eval("W.begin('discover')") is True


def test_beginning_a_request_clears_the_last_error(ctx) -> None:
    ctx.eval("mountProbe();")
    ctx.eval("W.say(new window.primerApi.ApiError(" + json.dumps(_refusal(500, "the database went away")) + ")); MR.rerender();")
    assert "database went away" in ctx.eval("W.error")
    ctx.eval("W.begin('discover'); MR.rerender();")
    assert ctx.eval("W.error") is None


def test_say_reads_a_refusal_in_the_one_readers_words(ctx) -> None:
    ctx.eval("mountProbe();")
    want = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(_refusal(422, 'bad id'))})).message")
    ctx.eval(f"W.say(new window.primerApi.ApiError({json.dumps(_refusal(422, 'bad id'))})); MR.rerender();")
    assert ctx.eval("W.error") == want and "bad id" in want
    ctx.eval("W.say(new Error('plain')); MR.rerender();")
    assert "plain" in ctx.eval("W.error")


def test_nothing_is_set_on_a_page_that_has_left(ctx) -> None:
    """A request answered after the overlay was closed must not touch state (``live`` goes false when the page unmounts)."""
    ctx.eval("mountProbe(); W.begin('discover'); var OLD = W; MR.mount(function Closed() { return null; }, {});")
    assert ctx.eval("OLD.live.current") is False
    renders = ctx.eval("RENDERS")
    ctx.eval("OLD.say(new Error('late')); OLD.end();")
    assert ctx.eval("RENDERS") == renders


def test_showing_the_other_page_asks_the_body_for_a_hand_off(ctx) -> None:
    """The button that took the page forward is gone when the other page is drawn: the page asks its body (the only thing that can act once the body has been drawn)."""
    ctx.eval("mountProbe();")
    assert ctx.eval("W.step") == 1 and js(ctx, "W.handoff") == {"n": 0, "to": "first"}, "opening is the dialog's own business"
    ctx.eval("W.say(new Error('old')); W.show(2, 'head'); MR.rerender();")
    assert ctx.eval("W.step") == 2 and js(ctx, "W.handoff") == {"n": 1, "to": "head"} and ctx.eval("W.error") is None
    ctx.eval("W.show(1, 'first'); MR.rerender();")
    assert ctx.eval("W.step") == 1 and js(ctx, "W.handoff") == {"n": 2, "to": "first"}


def test_the_kinds_are_the_ones_the_class_serves(ctx) -> None:
    from tests.ui._register_harness import types_of

    ctx.eval(f"TYPES.embedding_providers = {json.dumps(types_of('embedding_providers'))};")
    ctx.eval("mountProbe({ plural: 'embedding_providers' });")
    assert ctx.eval("W.types.types") is None and ctx.eval("W.types.loading") is True, "nothing has arrived yet"
    ctx.eval("serveTypes('embedding_providers');")
    assert js(ctx, "Object.keys(W.types.types)") == ["openai", "huggingface", "gemini"]


def test_a_try_again_that_worked_hands_the_focus_to_the_first_field(ctx) -> None:
    """The Try again button is gone once the fields arrive: the page asks its body to take the focus to the first field, once."""
    from tests.ui._register_harness import types_of

    ctx.eval(f"TYPES.embedding_providers = {json.dumps(types_of('embedding_providers'))};")
    key = "PC_typesKey('embedding_providers')"
    ctx.eval("mountProbe({ plural: 'embedding_providers' });")
    ctx.eval(f"fail({key}, {json.dumps(_refusal(500))}); MR.rerender();")
    assert ctx.eval("!!W.types.error && !W.types.types") is True
    n = ctx.eval("W.handoff.n")
    refetched = ctx.eval(f"RES[{key}].refetched")
    ctx.eval("W.retryTypes(); MR.rerender();")
    assert ctx.eval(f"RES[{key}].refetched") == refetched + 1, "Try again asks for the fields once more"
    ctx.eval("serveTypes('embedding_providers');")
    assert ctx.eval("W.handoff.n") == n + 1 and ctx.eval("W.handoff.to") == "first"
    ctx.eval("MR.rerender();")
    assert ctx.eval("W.handoff.n") == n + 1, "once per retry"


def test_a_try_again_is_not_asked_twice_while_the_first_is_out(ctx) -> None:
    ctx.eval("mountProbe({ plural: 'embedding_providers' });")
    key = "PC_typesKey('embedding_providers')"
    ctx.eval(f"fail({key}, {json.dumps(_refusal(500))}, {{ loading: true }}); MR.rerender();")
    refetched = ctx.eval(f"RES[{key}].refetched")
    ctx.eval("W.retryTypes();")
    assert ctx.eval(f"RES[{key}].refetched") == refetched


# ---- the hand-off of the focus -----------------------------------------------------------------------------------------------------------------------

FOCUS_STUB = r"""
var FOCUSED = [];
var HEADING = { focus: function () { FOCUSED.push('heading'); } };
var FIRST = { focus: function () { FOCUSED.push('first'); } };
var ROOT = { querySelector: function (selector) { return selector === '[data-pc-focus]' ? HEADING : FIRST; } };
var ELSEWHERE = { id: 'a-button-the-operator-chose' };
function focusing(props) { MR.mount(PC_PageFocus, Object.assign({ rootRef: { current: ROOT } }, props)); }
"""


def test_the_hand_off_acts_only_when_the_focus_was_lost(ctx) -> None:
    """``PC_PageFocus`` with a document of its own: it moves the focus when it is on ``<body>`` (the button it was on was removed), never when the operator has put it
    somewhere. The journey that drives it in a browser is ``tests/ui_e2e/test_register_models_speech_focus_journey.py``."""
    ctx.eval(FOCUS_STUB)
    ctx.eval("document.activeElement = document.body; focusing({ n: 1, to: 'head' });")
    assert js(ctx, "FOCUSED") == ["heading"]
    ctx.eval("FOCUSED = []; document.activeElement = document.body; focusing({ n: 2, to: 'first' });")
    assert js(ctx, "FOCUSED") == ["first"]
    ctx.eval("FOCUSED = []; document.activeElement = ELSEWHERE; focusing({ n: 3, to: 'head' });")
    assert js(ctx, "FOCUSED") == [], "a focus the operator put somewhere is left where it is"
    ctx.eval("FOCUSED = []; document.activeElement = document.body; focusing({ n: 0, to: 'head' });")
    assert js(ctx, "FOCUSED") == [], "n is 0 until a hand-off is asked for (the first draw of the page)"
    ctx.eval("FOCUSED = []; focusing({ n: 4, to: 'head' }); MR.rerender(); MR.rerender();")
    assert js(ctx, "FOCUSED") == ["heading"], "once per request, not once per render"
    ctx.eval("FOCUSED = []; MR.mount(PC_PageFocus, { n: 5, to: 'head', rootRef: { current: null } });")
    assert js(ctx, "FOCUSED") == [], "a body with no root (not drawn) has nothing to focus"


def test_the_first_field_is_the_first_one_a_person_can_type_in(ctx) -> None:
    """``first`` asks for an input, select or textarea that is not disabled and not read-only (a locked id is not where the focus belongs)."""
    ctx.eval(
        "var ASKED = []; var ROOT2 = { querySelector: function (s) { ASKED.push(s); return { focus: function () {} }; } };"
        "document.activeElement = document.body; MR.mount(PC_PageFocus, { n: 1, to: 'first', rootRef: { current: ROOT2 } });"
    )
    asked = js(ctx, "ASKED")[0]
    for part in ("input:not([disabled])", ":not([readonly])", "select:not([disabled])", "textarea:not([disabled])"):
        assert part in asked, part


# ---- one served field ---------------------------------------------------------------------------------------------------------------------------------

FIELD_STUB = r"""
var CHANGED = [];
function mountField(props) {
  MR.mount(function Host(p) { return el("div", { "data-testid": "host" }, el(PC_ServedField, Object.assign({ onChange: function (v) { CHANGED.push(v); } }, p))); }, props);
}
"""


def _mount_field(ctx, **props) -> None:
    ctx.eval(FIELD_STUB)
    ctx.eval(f"mountField({json.dumps(props)});")


def test_a_served_field_is_a_kit_field_with_the_label_alone_and_the_help_under_the_control(ctx) -> None:
    _mount_field(ctx, field={"key": "url", "label": "Base URL", "type": "url", "required": True, "placeholder": "https://x/v1"}, value="http://a/v1")
    node = js(ctx, "(function () { var n = MR.find('pc-field:url'); return { type: n.type, inputType: n.props.type, value: n.props.value, ph: n.props.placeholder, req: n.props['aria-required'] }; })()")
    assert node == {"type": "input", "inputType": "url", "value": "http://a/v1", "ph": "https://x/v1", "req": "true"}
    assert "URL" in ctx.eval("MR.texts().join('|')") and "Base URL" not in ctx.eval("MR.texts().join('|')"), "the console's name for the field wins over the served label"


def test_a_label_and_help_of_the_pages_own_win(ctx) -> None:
    _mount_field(ctx, field={"key": "token", "label": "HF token (optional for public repos)", "type": "password", "required": False, "help": "served words"},
                 value="", label="Own label", help="Own help")
    texts = ctx.eval("MR.texts().join('|')")
    assert "Own label" in texts and "Own help" in texts and "served words" not in texts and "HF token" not in texts


def test_the_served_help_is_kept_unless_the_page_gives_its_own(ctx) -> None:
    _mount_field(ctx, field={"key": "api_key", "label": "API key (optional)", "type": "password", "required": False, "help": "served words"}, value="")
    assert "served words" in ctx.eval("MR.texts().join('|')")
    _mount_field(ctx, field={"key": "api_key", "label": "API key (optional)", "type": "password", "required": False, "help": "served words"}, value="", help=None)
    assert "served words" not in ctx.eval("MR.texts().join('|')"), "a page can say there is no help at all"


def test_a_secret_in_an_edit_says_it_is_kept_when_left_as_it_is(ctx) -> None:
    field = {"key": "api_key", "label": "API key", "type": "password", "required": False, "help": "Optional for local servers"}
    _mount_field(ctx, field=field, value="**********abcd", editing=True)
    texts = ctx.eval("MR.texts().join('|')")
    assert "Optional for local servers Stored secrets are shown masked; leave it as is to keep it." in texts
    assert ctx.eval("MR.find('pc-field:api_key').props.type") == "password" and ctx.eval("MR.find('pc-field:api_key').props.autoComplete") == "off"
    _mount_field(ctx, field=field, value="", editing=False)
    assert "Stored secrets" not in ctx.eval("MR.texts().join('|')")


def test_an_enum_is_a_select_of_the_served_options_named_by_the_consoles_words(ctx) -> None:
    field = {"key": "flavor", "label": "Flavor", "type": "enum", "required": False, "options": ["openai", "lmstudio", "vllm", "other"]}
    _mount_field(ctx, field=field, value="other")
    assert ctx.eval("MR.find('pc-field:flavor').type") == "select" and ctx.eval("MR.find('pc-field:flavor').props.value") == "other"
    labels = js(ctx, "ALL('pc-field:flavor').filter(function (n) { return n.type === 'option'; }).map(function (n) { return textOf(n); })")
    assert labels == ["OpenAI", "LM Studio", "vLLM", "Other"], "no 'Not set' once a choice is made"
    ctx.eval("MR.find('pc-field:flavor').props.onChange({ target: { value: 'vllm' } });")
    assert js(ctx, "CHANGED") == ["vllm"]


def test_an_optional_enum_with_no_choice_reads_not_set(ctx) -> None:
    """Showing the first option while sending nothing would show a value that is never sent."""
    field = {"key": "mode", "label": "Mode", "type": "enum", "required": False, "options": ["a", "b"]}
    _mount_field(ctx, field=field, value=None)
    values = js(ctx, "ALL('pc-field:mode').filter(function (n) { return n.type === 'option'; }).map(function (n) { return n.props.value; })")
    assert values == ["", "a", "b"]
    _mount_field(ctx, field=dict(field, required=True), value=None)
    values = js(ctx, "ALL('pc-field:mode').filter(function (n) { return n.type === 'option'; }).map(function (n) { return n.props.value; })")
    assert values == ["a", "b"], "a required choice is never 'Not set'"


def test_the_help_sits_under_the_control_and_the_label_is_alone_on_its_line(ctx) -> None:
    _mount_field(ctx, field={"key": "api_key", "label": "API key (optional)", "type": "password", "required": False, "help": "Optional for local servers"}, value="")
    row = js(ctx, "fieldRow('host', 'pc-field:api_key')")
    assert [c["type"] for c in row] == ["label", "input", "div"], "the label, the control, then the help"
    assert row[0]["text"] == "API key" and row[2]["text"] == "Optional for local servers" and row[2]["className"] == "field-help"


def test_span_full_takes_the_whole_row(ctx) -> None:
    _mount_field(ctx, field={"key": "url", "label": "url", "type": "url", "required": True}, value="", span="full")
    wide = js(ctx, "ALL('host').filter(function (n) { return n.type === 'div' && String(n.props.className || '').indexOf('pk-span-full') >= 0; }).length")
    assert wide == 1
    _mount_field(ctx, field={"key": "url", "label": "url", "type": "url", "required": True}, value="")
    assert js(ctx, "ALL('host').filter(function (n) { return n.type === 'div' && String(n.props.className || '').indexOf('pk-span-full') >= 0; }).length") == 0


# ---- the lines that say the fields are loading or could not be loaded ---------------------------------------------------------------------------------

NOTICE_STUB = r"""
var RETRIED = 0;
function mountNotice(props) {
  MR.mount(function Host(p) { return el("div", { "data-testid": "host" }, el(PC_TypesNotice, Object.assign({ onRetry: function () { RETRIED += 1; } }, p))); }, props);
}
"""


def test_the_notice_says_while_the_kinds_are_loading(ctx) -> None:
    ctx.eval(NOTICE_STUB)
    ctx.eval("mountNotice({ loading: true, failed: false, notServed: false, types: { loading: true } });")
    node = js(ctx, "(function () { var n = MR.find('pc-register-loading'); return { text: textOf(n), role: n.props.role }; })()")
    assert node == {"text": "Loading the fields for this kind…", "role": "status"}
    assert ctx.eval("MR.find('pc-register-types-error') === null && MR.find('pc-register-kind-missing') === null") is True


def test_the_notice_is_an_alert_with_a_try_again_that_says_it_is_trying(ctx) -> None:
    ctx.eval(NOTICE_STUB)
    error = _refusal(500, "the registry is down")
    ctx.eval(f"mountNotice({{ failed: true, types: {{ error: new window.primerApi.ApiError({json.dumps(error)}), loading: false }} }});")
    assert ctx.eval("MR.find('pc-register-types-error').props.role") == "alert"
    assert "Could not load the fields for this kind: " in ctx.eval("textOf(MR.find('pc-register-types-error'))")
    assert "the registry is down" in ctx.eval("textOf(MR.find('pc-register-types-error'))")
    assert ctx.eval("textOf(MR.find('pc-register-types-retry'))") == "Try again"
    ctx.eval("MR.click('pc-register-types-retry')")
    assert ctx.eval("RETRIED") == 1
    ctx.eval(f"mountNotice({{ failed: true, types: {{ error: new window.primerApi.ApiError({json.dumps(error)}), loading: true }} }});")
    assert ctx.eval("textOf(MR.find('pc-register-types-retry'))") == "Trying again…"
    button = "ALL('pc-register-types-retry').filter(function (n) { return n.type === 'button'; })[0]"
    assert ctx.eval(f"{button}.props['aria-busy']") == "true", "it stays focusable and says it is busy"


def test_the_notice_names_a_kind_the_install_does_not_serve(ctx) -> None:
    ctx.eval(NOTICE_STUB)
    ctx.eval("mountNotice({ notServed: true, kind: 'no-such-kind', types: {} });")
    text = ctx.eval("textOf(MR.find('pc-register-kind-missing'))")
    assert 'does not serve the kind "no-such-kind"' in text and ctx.eval("MR.find('pc-register-kind-missing').props.role") == "alert"
    ctx.eval("mountNotice({ notServed: true, kind: '', types: {} });")
    assert "serves no kind for this class" in ctx.eval("textOf(MR.find('pc-register-kind-missing'))")


def test_a_notice_with_nothing_to_say_draws_nothing(ctx) -> None:
    ctx.eval(NOTICE_STUB)
    ctx.eval("mountNotice({ types: {} });")
    assert js(ctx, "testids('host')") == ["host"] and ctx.eval("textOf(MR.find('host'))") == ""


# ---- the heading of a second page ---------------------------------------------------------------------------------------------------------------------


def test_the_heading_of_a_second_page_can_take_the_focus(ctx) -> None:
    ctx.eval("MR.mount(PC_PageHead, { title: 'Models found at emb', sub: '✓ Connected; 3 models listed' });")
    head = js(ctx, "(function () { var n = MR.find('pc-import-head'); return { tab: n.props.tabIndex, mark: n.props['data-pc-focus'], text: textOf(n) }; })()")
    assert head == {"tab": -1, "mark": "true", "text": "Models found at emb✓ Connected; 3 models listed"}


def test_the_heading_says_in_what_tone_the_second_line_is(ctx) -> None:
    ctx.eval("MR.mount(PC_PageHead, { title: 't', sub: 's', tone: 'warn' });")
    assert ctx.eval("ALL('pc-import-head').filter(function (n) { return n.props['data-tone']; })[0].props['data-tone']") == "warn"
