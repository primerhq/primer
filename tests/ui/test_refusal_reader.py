"""ONE reader for a refused write (ticket 01a11cd1-7aaf), and the two refusals it makes readable (admin review ADM-12 and ADM-20).

Seven components each read a failed write out of the RFC 7807 envelope their own way (``MC_extractError``, ``AT_extractError``, ``ADM_extractError``,
``SSO_extractError``, ``SV_extractError``, ``LA_extractError`` and the trigger dialogs' own reader), and they disagreed: the trigger reader knew the auth gate puts its code in ``extensions.error`` and sends no
message, the others printed the gate's bare ``auth_required`` as the message, and ``SSO_extractError`` still looked for the code in a ``detail`` object
that the handler never sends (so its code was always null). ``window.primerApi.readRefusal`` (``ui/foundation/api.js``, next to ``ApiError``) is the one
reader: ``{code, field, sentence, message}``.

* ``code``: ``extensions.code`` (routers raise ``HTTPException(detail={code, message})``), ``extensions.error`` (the auth gate, and the profile pre-write
  422s ``{error, field, message}``), ``in_use_by`` for a reference block, an older ``detail`` object, or a ``detail`` that is only a snake_case code. A request-validation
  error (``extensions.errors[]``) has NO code: its ``type`` is a pydantic type or the name of a check (``string_too_short``), which a banner must not print as a code.
* ``field``: ``extensions.field`` or the dotted path of the first request-validation error (``body.`` dropped; an entry that is not an object has none).
* ``sentence``: the server's own words, or ``""`` when it sent none or only the code. A message equal to the code is not a sentence; one that merely looks like a
  code is kept when the code is known and different (the not-found message is a bare id, and an id may hold an underscore). Exactly one request-validation error
  is said in its own ``msg`` (the ``message`` names the field first, ``name: Field required``, because a generic toast has no field to draw it under; a caller that
  draws the error under the field uses the bare ``sentence``); several are not (``Missing or invalid: a, b.``).
* ``message``: what a person reads. The sentence, except that a reference block (``in_use_by: 1 agent(s) reference 'x' (first: 'y')``) and a duplicate
  (``Channel with provider_id='a', external_id='b' already exists (id='c')``) are said in plain words (ADM-12, ADM-20); with no sentence, a known bare code
  (``auth_required``, ``forbidden_role``) gets its own sentence, else the HTTP title with the code after it (``Forbidden (scope_required)``), then the error's
  message, then the caller's fallback. A caller that must not show a code (the trigger banners: a code never goes in a title, only the HTTP title does) passes
  ``{codeAfterTitle: false}``.

The envelopes are the REAL ones (``tests/_support/refusal_envelopes.py``: the real handlers and the real producers answer through ``TestClient``), and the
browser side is the real ``ui/foundation/api.js`` and the real component functions in MiniRacer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


@pytest.fixture(scope="module")
def envelopes() -> dict[str, dict]:
    from tests._support.refusal_envelopes import refusal_envelopes

    return refusal_envelopes()


def _context(*sources: str):
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = {};")
    ctx.eval(API)
    for src in sources:
        ctx.eval(src)
    return ctx


def _function(path: str, name: str) -> str:
    """The source of ``function name(...) { ... }`` in a component, by brace counting (these functions hold no braces inside strings)."""
    text = (UI / path).read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    depth, i = 0, text.index("{", start)
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def _read(ctx, envelope: dict, fallback: str | None = None, options: dict | None = None) -> dict:
    arg = json.dumps(fallback)
    opts = json.dumps(options)
    return json.loads(ctx.eval(f"JSON.stringify(window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelope)}), {arg}, {opts}))"))


# ---- the reader, on every shape the API really answers ----------------------------------------------------------------------------------------------------

_IN_USE = "'llm-openchat--scripted:default' is still in use by an agent, for example builder. Remove or change that first."
_IN_USE_SESSION = "'ag-1' is still in use by a session, for example sess-0001. Remove or change that first."
_IN_USE_AGGREGATE = "'p-1' is still in use by a model profile (aggregate member), for example agg-1. Remove or change that first."
_CHANNEL = "A channel with provider_id rev-slack and external_id C0AAAA0001 already exists: channel-fbf469c47c2a."
_NO_PROFILE = "ModelProfile 'p-9' does not exist; create the profile first or name an existing one"
_BLANK_DESCRIPTION = "the description must not be blank: other agents find an agent by its description"

EXPECTED = {
    "session_ended": {"code": "auth_required", "field": None, "sentence": "", "message": "Your session has ended; sign in again."},
    "role_refused": {"code": "forbidden_role", "field": None, "sentence": "", "message": "Your role does not allow this."},
    "router_code": {
        "code": "trigger_slug_conflict", "field": None, "sentence": "slug 'nightly' already in use", "message": "slug 'nightly' already in use",
    },
    "router_code_bare_id": {"code": "trigger_not_found", "field": None, "sentence": "nightly_job", "message": "nightly_job"},
    "pre_write": {"code": "model_profile_not_found", "field": "model.profile_id", "sentence": _NO_PROFILE, "message": _NO_PROFILE},
    "agent_field": {"code": None, "field": "description", "sentence": _BLANK_DESCRIPTION, "message": "description: " + _BLANK_DESCRIPTION},
    "validated": {"code": None, "field": "name", "sentence": "", "message": "Missing or invalid: name, count."},
    "validated_missing": {"code": None, "field": "name", "sentence": "Field required", "message": "name: Field required"},
    "in_use_by": {
        "code": "in_use_by", "field": None,
        "sentence": "in_use_by: 1 agent(s) reference 'llm-openchat--scripted:default' (first: 'builder')", "message": _IN_USE,
    },
    "in_use_by_session": {
        "code": "in_use_by", "field": None, "sentence": "in_use_by: 1 session(s) reference 'ag-1' (first: 'sess-0001')", "message": _IN_USE_SESSION,
    },
    "in_use_by_aggregate": {
        "code": "in_use_by", "field": None,
        "sentence": "in_use_by: 1 model_profile (aggregate member)(s) reference 'p-1' (first: 'agg-1')", "message": _IN_USE_AGGREGATE,
    },
    "channel_conflict": {
        "code": None, "field": None,
        "sentence": "Channel with provider_id='rev-slack', external_id='C0AAAA0001' already exists (id='channel-fbf469c47c2a')", "message": _CHANNEL,
    },
    "not_found": {"code": None, "field": None, "sentence": "Agent 'ag-9' not found", "message": "Agent 'ag-9' not found"},
    "provider_error": {"code": None, "field": None, "sentence": "the provider answered 502", "message": "the provider answered 502"},
}


def test_the_expected_table_covers_every_real_envelope(envelopes) -> None:
    """The default-agent refusal is also a reference block, but its wording changes with the default-agent PR, so it has its own test below."""
    assert set(EXPECTED) | {"default_agent"} == set(envelopes)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_reader_reads_every_real_envelope(envelopes, name: str) -> None:
    got = _read(_context(), envelopes[name])

    assert got == EXPECTED[name]


def test_the_real_envelopes_really_look_like_this(envelopes) -> None:
    """The premise: if the server's shapes changed, the table above would pass for nothing."""
    gate, field, pre, block = envelopes["session_ended"], envelopes["agent_field"], envelopes["pre_write"], envelopes["in_use_by"]

    assert gate["detail"] == "auth_required" and gate["extensions"] == {"error": "auth_required"}
    assert field["extensions"]["errors"][0]["type"] == "agent_description_blank" and field["extensions"]["errors"][0]["loc"] == ["body", "description"]
    assert field["extensions"]["errors"][0]["msg"] == _BLANK_DESCRIPTION
    assert pre["extensions"]["error"] == "model_profile_not_found" and pre["extensions"]["field"] == "model.profile_id"
    assert block["status"] == 409 and "extensions" not in block and block["detail"].startswith("in_use_by: ")


def test_the_default_agent_block_is_a_reference_block_said_without_the_token(envelopes) -> None:
    got = _read(_context(), envelopes["default_agent"])

    assert got["code"] == "in_use_by"
    assert got["sentence"].startswith("in_use_by: agent 'ag-1' is the system default agent")
    assert got["message"].startswith("Agent 'ag-1' is the system default agent")
    assert "in_use_by" not in got["message"]


# ---- what it falls back to --------------------------------------------------------------------------------------------------------------------------------


def test_a_message_equal_to_the_code_is_not_a_sentence() -> None:
    env = {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": "something_unheard_of", "extensions": {"code": "something_unheard_of"}}

    assert _read(_context(), env) == {"code": "something_unheard_of", "field": None, "sentence": "", "message": "Conflict (something_unheard_of)"}


def test_a_detail_that_is_only_a_snake_case_code_is_the_code() -> None:
    env = {"type": "/errors/bad-request", "title": "Bad Request", "status": 400, "detail": "payload_malformed"}

    assert _read(_context(), env) == {"code": "payload_malformed", "field": None, "sentence": "", "message": "Bad Request (payload_malformed)"}


def test_an_older_detail_object_still_reads() -> None:
    env = {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": {"code": "old_shape", "message": "the old way"}}

    got = _read(_context(), env)

    assert got["code"] == "old_shape" and got["sentence"] == "the old way" and got["message"] == "the old way"


def test_a_failure_with_no_envelope_uses_the_error_message_then_the_fallback() -> None:
    ctx = _context()

    plain = json.loads(ctx.eval('JSON.stringify(window.primerApi.readRefusal(new Error("boom"), "Save failed"))'))
    nothing = json.loads(ctx.eval('JSON.stringify(window.primerApi.readRefusal(undefined, "Save failed"))'))
    empty = json.loads(ctx.eval('JSON.stringify(window.primerApi.readRefusal({}, undefined))'))

    assert plain == {"code": None, "field": None, "sentence": "", "message": "boom"}
    assert nothing == {"code": None, "field": None, "sentence": "", "message": "Save failed"}
    assert empty == {"code": None, "field": None, "sentence": "", "message": "Request failed"}


def test_a_network_error_says_what_the_browser_said() -> None:
    env = {"type": "/errors/network-error", "title": "Network error", "status": 0, "detail": "Failed to fetch"}

    got = _read(_context(), env)

    assert got["sentence"] == "Failed to fetch" and got["message"] == "Failed to fetch"


# ---- ADM-12 and ADM-20: the two refusals in plain words ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detail", "message"),
    [
        ("in_use_by: 1 agent(s) reference 'p-1' (first: 'builder')", "'p-1' is still in use by an agent, for example builder. Remove or change that first."),
        ("in_use_by: 1 graph(s) reference 'ag-1' (first: 'g-1')", "'ag-1' is still in use by a graph, for example g-1. Remove or change that first."),
        (
            "in_use_by: 1 trigger subscription(s) reference 'ag-1' (first: 'sub-1')",
            "'ag-1' is still in use by a trigger subscription, for example sub-1. Remove or change that first.",
        ),
        ("in_use_by: 1 session(s) reference \"it's\" (first: 's-1')", "\"it's\" is still in use by a session, for example s-1. Remove or change that first."),
        ("in_use_by: something this reader has never seen", "Something this reader has never seen"),
        # a kind is a Python identifier on the server: said as words (ADM-12, review of #625)
        (
            "in_use_by: 1 tool_approval_policy(s) reference 'x' (first: 'p-1')",
            "'x' is still in use by a tool approval policy, for example p-1. Remove or change that first.",
        ),
        (
            "in_use_by: 1 workspace_template(s) reference 'w-1' (first: 'ag-1')",
            "'w-1' is still in use by a workspace template, for example ag-1. Remove or change that first.",
        ),
        (
            "in_use_by: 1 model_profile (aggregate member)(s) reference 'p-1' (first: 'agg-1')",
            "'p-1' is still in use by a model profile (aggregate member), for example agg-1. Remove or change that first.",
        ),
    ],
)
def test_a_reference_block_is_said_in_plain_words(detail: str, message: str) -> None:
    env = {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": detail}

    got = _read(_context(), env)

    assert got["message"] == message
    assert got["sentence"] == detail, "the server's own sentence stays available for a caller that wants to show it"


@pytest.mark.parametrize(
    ("detail", "message"),
    [
        (
            "Channel with provider_id='rev-slack', external_id='C0AAAA0001' already exists (id='channel-1')",
            "A channel with provider_id rev-slack and external_id C0AAAA0001 already exists: channel-1.",
        ),
        ("Agent with id 'ag-1' already exists", "Agent with id 'ag-1' already exists"),
        ("a token named 'ci' already exists", "a token named 'ci' already exists"),
        (
            "Collection with name='docs' already exists (id='c-1')",
            "A collection with name docs already exists: c-1.",
        ),
    ],
)
def test_a_duplicate_is_said_in_plain_words_and_other_sentences_are_left_alone(detail: str, message: str) -> None:
    env = {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": detail}

    assert _read(_context(), env)["message"] == message


# ---- the seven readers are the one reader -----------------------------------------------------------------------------------------------------------------

_EXTRACTORS = [
    ("components/mcp.jsx", "MC_extractError"),
    ("components/api_tokens.jsx", "AT_extractError"),
    ("components/admin_users.jsx", "ADM_extractError"),
    ("components/sso_admin.jsx", "SSO_extractError"),
    ("components/services.jsx", "SV_extractError"),
    ("components/linked_accounts.jsx", "LA_extractError"),
]


@pytest.mark.parametrize(("path", "name"), _EXTRACTORS)
@pytest.mark.parametrize("envelope_name", sorted(EXPECTED))
def test_a_component_reader_gives_what_the_one_reader_gives(envelopes, path: str, name: str, envelope_name: str) -> None:
    ctx = _context(_function(path, name))
    envelope = json.dumps(envelopes[envelope_name])

    got = json.loads(ctx.eval(f"JSON.stringify({name}(new window.primerApi.ApiError({envelope})))"))

    assert got == {"code": EXPECTED[envelope_name]["code"], "message": EXPECTED[envelope_name]["message"]}


@pytest.mark.parametrize(("path", "name"), _EXTRACTORS)
def test_no_component_reader_prints_the_auth_gates_bare_code(envelopes, path: str, name: str) -> None:
    ctx = _context(_function(path, name))

    for key in ("session_ended", "role_refused"):
        got = json.loads(ctx.eval(f"JSON.stringify({name}(new window.primerApi.ApiError({json.dumps(envelopes[key])})))"))
        assert got["message"] not in ("auth_required", "forbidden_role"), (name, key, got)


# One rule: a component never reads ``.envelope`` (the thrown ApiError's problem document). Everything it needs from a refusal is in ``window.primerApi.readRefusal``.
# The first version of this guard listed the fields (``ext.code``, ``envDetail.error`` ...) and missed two copies of the reader (review of #625); a rule about the one
# property they all go through cannot be walked around by renaming a variable or adding ``?.``.
_ENVELOPE_READ = re.compile(r"\.envelope\b|\{\s*envelope\b")


def _reads_the_envelope(line: str) -> bool:
    return _ENVELOPE_READ.search(line.split("//", 1)[0]) is not None


@pytest.mark.parametrize(
    "line",
    [
        "const f = err.envelope?.extensions?.field;",
        "const env = err && err.envelope;",
        "var ext = ((err && err.envelope && err.envelope.extensions) || {});",
        "const { envelope } = err;",
        "code = e.envelope.detail?.code;",
        # the shapes the first scan walked around (the #625 review): a bracket access, a destructuring that does not start with it, one over several lines,
        # an alias, a parameter, a loop, a read on the line after a block comment that mentions it
        "const e = err['envelope'].extensions;",
        'const e = err?.["envelope"];',
        "const { message, envelope } = err;",
        "const {\n  message,\n  title,\n  envelope,\n} = err;",
        "const { envelope: env } = err;",
        "function Handler({ onClose, envelope }) {",
        "const show = ({ envelope }) => envelope.detail;",
        "for (const { envelope } of errors) {",
        "/* the reader does this */\nconst e = err.envelope;",
    ],
)
def test_the_scan_sees_a_component_read_the_envelope(line: str) -> None:
    assert _reads_the_envelope(line)


@pytest.mark.parametrize(
    "line",
    [
        "const r = window.primerApi.readRefusal(err);",
        "return { code: r.code, message: r.message };",
        "// err.envelope.extensions.code is read by the reader",
        'title="the problem envelope of a failed write"',
        "const item = list.items.find((x) => x.id === id);",
        "/* err.envelope.extensions.code\n   is read by the reader */",
        "// const { message, envelope } = err;",
        "const note = 'see err.envelope in the docs';",
        'const url = "http://example.com/a"; const ok = { envelope: 1 };',
        "const { message, title } = err;",
        "const extra = { envelopes: [] };",
    ],
)
def test_the_scan_leaves_the_reader_and_prose_alone(line: str) -> None:
    assert not _reads_the_envelope(line)


# The Python editor shows the registration message of a refused toolset save INLINE, under the line and function it names: that one field is not in
# ``readRefusal``'s result, so the editor reads ``err.envelope.extensions`` for it (ui/components/toolsets/python-editor.jsx).
_MAY_READ_THE_ENVELOPE = {"components/toolsets/python-editor.jsx"}


def test_no_component_reads_the_envelope_but_the_python_editor() -> None:
    offenders = []
    for path in sorted((UI / "components").rglob("*.jsx")):
        rel = path.relative_to(UI).as_posix()
        if rel in _MAY_READ_THE_ENVELOPE or rel.endswith("mock-data.jsx"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _reads_the_envelope(line):
                offenders.append(f"{rel}:{number}: {line.strip()[:110]}")
    assert not offenders, "a component reads a refusal out of the envelope itself instead of window.primerApi.readRefusal:\n" + "\n".join(offenders)


# A code never goes in a banner TITLE (ruling 01a11bf7-15b7, restated in the #625 review): the title is "Create failed" or "Save failed"; when the server sent no
# sentence the MESSAGE carries the code (``Forbidden (scope_required)``). The one reader finds codes the old copies missed, so a title that composes one would put
# ``Save failed (auth_required)`` where main said ``Save failed``.
_TITLE_WITH_CODE = [
    re.compile(r"\btitle=\{[^}]*?\.code\b"),      # title={error.code ? `Save failed (${error.code})` : "Save failed"}, also over several lines
    re.compile(r"\btitle\s*:\s*[^,\n}]*\.code\b"),   # a toast or banner object: { title: `Failed (${err.code})` }
]


def _titles_that_compose_a_code(text: str) -> list[int]:
    code = "\n".join(line.split("//", 1)[0] for line in text.split("\n"))
    return sorted({code.count("\n", 0, m.start()) + 1 for pattern in _TITLE_WITH_CODE for m in pattern.finditer(code)})


@pytest.mark.parametrize(
    "text",
    [
        'title={error.code ? `Save failed (${error.code})` : "Save failed"}',
        "title={toggleError.code\n  ? `Toggle failed (${toggleError.code})`\n  : \"Toggle failed\"}",
        "pushToast({ title: `Failed (${err.code})`, kind: \"error\" })",
        "title={submitError && submitError.code ? submitError.code : \"x\"}",
        # a `}` of a template placeholder before the code ended the first scan's match
        'title={isEdit ? `Save (${name}) failed (${error.code})` : "Save failed"}',
        "pushToast({ title: isEdit ? `Failed (${a})` : `Failed (${err.code})`, kind: \"error\" })",
        # a destructured code variable, a bracket access, the code on a later line
        'const { code } = refusal;\n<Banner title={code ? `Save failed (${code})` : "Save failed"} />',
        'title={`Save failed (${err["code"]})`}',
        "title={\n  isEdit\n    ? \"Save failed\"\n    : `Create failed (${\n      err.code\n    })`\n}",
        "pushToast({\n  kind: \"error\",\n  title: `Failed (${r.code})`,\n})",
    ],
)
def test_the_title_scan_sees_a_code_in_a_title(text: str) -> None:
    assert _titles_that_compose_a_code(text)


@pytest.mark.parametrize(
    "text",
    [
        'title="Create failed"',
        "title={`${isEdit ? \"Save\" : \"Create\"} failed`}",
        "// title={error.code ? `Save failed (${error.code})` : \"Save failed\"}",
        "detail={error.code ? error.code : \"\"}",
        "<Chip title=\"x\" label={row.code} />",
        # the word "code" in the words of a title is not a code; the value of `title:` ends at its comma
        'title={"Verification code"}',
        "title={`Invalid code ${n}`}",
        'pushToast({ title: "Code review", detail: err.code })',
        "title={`${a} of ${b} failed`}",
        'const { code } = refusal;\n<Banner title="Save failed" detail={code} />',
    ],
)
def test_the_title_scan_leaves_plain_titles_and_other_props_alone(text: str) -> None:
    assert not _titles_that_compose_a_code(text)


def test_no_banner_title_in_any_component_composes_a_code() -> None:
    offenders = []
    for path in sorted((UI / "components").rglob("*.jsx")):
        rel = path.relative_to(UI).as_posix()
        if rel.endswith("mock-data.jsx"):
            continue
        offenders += [f"{rel}:{n}" for n in _titles_that_compose_a_code(path.read_text(encoding="utf-8"))]
    assert not offenders, "a title puts the code in the banner title (the message carries it when there is no sentence):\n" + "\n".join(offenders)


@pytest.mark.parametrize(("path", "name"), [("components/services.jsx", "SV_extractError"), ("components/linked_accounts.jsx", "LA_extractError")])
def test_the_two_copies_the_first_scan_missed_are_wrappers_over_the_reader(path: str, name: str) -> None:
    source = _function(path, name)

    assert "window.primerApi.readRefusal(" in source and len(source.splitlines()) <= 4, source


_CHANNELS_PRELUDE = """
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
globalThis.Btn = function (props) { return React.createElement("button", { "data-testid": "btn-" + props.kind, onClick: props.onClick, disabled: props.disabled }, props.children); };
globalThis.Icon = function () { return null; };
// FormField is the labelled row the dialog's fields are drawn with (console review C-003); this test only presses Save, so a pass-through row is all it needs.
globalThis.FormField = function (props) { return React.createElement("div", null, props.children); };
var __toasts = [];
var __failure = null;
window.primerApi = window.primerApi || {};
window.primerApi.apiFetch = function () { return Promise.reject(__failure); };
window.primerApi.useResource = function () { return { data: null, loading: false, error: null, refetch: function () {} }; };
"""


@pytest.mark.parametrize("envelope_name", ["channel_conflict", "session_ended", "not_found"])
def test_the_new_channel_dialog_toasts_a_refusal_through_the_one_reader(envelopes, envelope_name: str) -> None:
    """ADM-20 in the real dialog: ``NewChannelModal`` has its own ``onError`` (so the default toast of ``useMutation`` is not used), and it printed
    ``err.detail`` as it came. Mounted with the real ``useMutation`` and a real ``ApiError``; a click on Save answers the REAL envelope."""
    from tests.ui._mini_react import mini_react_context, transpile

    source = transpile(UI / "components" / "channels.jsx")
    prelude = API + "\n" + (UI / "foundation" / "use-mutation.js").read_text(encoding="utf-8") + "\n" + _CHANNELS_PRELUDE
    ctx = mini_react_context(source, prelude)
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(f"__failure = new window.primerApi.ApiError({json.dumps(envelopes[envelope_name])});")
    existing = {"id": "ch-1", "provider": "discord", "provider_id": "cp-1", "external_id": "C1", "config": {"chats": {"enabled": False}}}
    ctx.eval(
        "MR.mount(NewChannelModal, { providers: [{ id: 'cp-1', provider: 'discord' }], onClose: function () {}, onCreated: function () {},"
        f" pushToast: function (t) {{ __toasts.push(t); }}, existing: {json.dumps(existing)} }})"
    )
    ctx.eval("MR.click('btn-primary')")
    ctx.eval("MR.rerender()")
    ctx.eval("MR.rerender()")
    toasts = json.loads(ctx.eval("JSON.stringify(__toasts)"))

    assert len(toasts) == 1, toasts
    assert toasts[0]["detail"] == EXPECTED[envelope_name]["message"], toasts[0]
    assert toasts[0]["kind"] == "error"


def test_the_trigger_load_banners_read_the_refusal_through_the_one_reader() -> None:
    """The list and the detail page printed ``error.detail || error.message``: a session that had ended read ``auth_required`` (review of #572)."""
    src = (UI / "components" / "triggers.jsx").read_text(encoding="utf-8")

    assert "window.primerApi.readRefusal(list.error).message" in src and "window.primerApi.readRefusal(detail.error).message" in src
    assert "list.error.detail ||" not in src and "detail.error.detail ||" not in src


@pytest.mark.parametrize(("envelope_name", "detail"), [("channel_conflict", _CHANNEL), ("validated_missing", "name: Field required")])
def test_the_default_error_toast_says_the_refusal_in_plain_words(envelopes, envelope_name: str, detail: str) -> None:
    """ADM-20: the toast a refused channel create raised read ``Channel with provider_id='rev-slack', external_id='C0AAAA0001' already exists (id='channel-...')``.
    A generic toast has no field to draw a validation error under, so a real one-missing-field 422 names the field (review of #625)."""
    src = (UI / "foundation" / "use-mutation.js").read_text(encoding="utf-8")
    ctx = _context()
    ctx.eval(
        """
        var pushed = [];
        window.primerApi.toastPush = function (t) { pushed.push(t); };
        window.React = { useState: function (v) { return [v, function () {}]; }, useRef: function (v) { return { current: v }; }, useCallback: function (f) { return f; } };
        """
    )
    ctx.eval(src)
    ctx.eval(
        f"""
        var mutation = window.primerApi.useMutation(function () {{ throw new window.primerApi.ApiError({json.dumps(envelopes[envelope_name])}); }});
        mutation.mutate({{}}).catch(function () {{}});
        """
    )
    ctx.eval("0")  # let the promise continuation run
    pushed = json.loads(ctx.eval("JSON.stringify(pushed)"))

    assert pushed and pushed[0]["detail"] == detail, pushed


# ---- review of #625: the rewrites keep to what they match, the code of a validation error, one entry, a malformed entry -----------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        "in_use_by: 1 agent(s) reference 'p-1' (first: 'a', 'b')",
        "in_use_by: 1 agent(s) reference 'p-1' (first: 'a'), 1 graph(s) reference 'p-1' (first: 'g')",
        "in_use_by: 1 agent(s) reference 'p-1' (first: 'a') and more",
    ],
)
def test_a_reference_block_with_more_in_it_than_one_id_is_left_in_the_servers_words(detail: str) -> None:
    """The ids are quoted: an id never holds the quote that closes it, so a second id or a second block is not part of the first one (the lazy capture swallowed it)."""
    env = {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": detail}

    message = _read(_context(), env)["message"]

    assert message == detail.removeprefix("in_use_by: "), message
    assert "for example" not in message


@pytest.mark.parametrize(
    "detail",
    [
        "Channel with provider_id='a' already exists (id='c-1'), another (id='c-2')",
        "Channel with provider_id='a' already exists (id='c-1') (id='c-2')",
        "Channel with provider_id='a' and external_id='b' already exists (id='c')",
        "Channel with provider_id='a', external_id=b already exists (id='c')",
    ],
)
def test_a_duplicate_that_is_not_exactly_one_pair_list_and_one_id_is_left_in_the_servers_words(detail: str) -> None:
    """The second id is not part of the first (the lazy capture swallowed it), and a pair list that does not read back as ``name='value', name='value'`` is not rewritten."""
    env = {"type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": detail}

    assert _read(_context(), env)["message"] == detail


@pytest.mark.parametrize(
    ("status", "title", "code"),
    [(422, "Validation Error", "managed_field_set"), (403, "Forbidden", "scope_required"), (409, "Conflict", "something_unheard_of")],
)
@pytest.mark.parametrize("where", ["code", "error"])
def test_a_refusal_with_a_code_and_no_sentence_names_the_code_after_the_title(status: int, title: str, code: str, where: str) -> None:
    """The server sent only a code: "Validation Error" alone tells nobody what was refused. A known bare code has its own sentence and does not get this."""
    env = {"type": "/errors/x", "title": title, "status": status, "detail": code, "extensions": {where: code}}

    got = _read(_context(), env)

    assert got["sentence"] == "" and got["message"] == f"{title} ({code})"


@pytest.mark.parametrize(
    ("status", "title", "code"),
    [(422, "Validation Error", "managed_field_set"), (403, "Forbidden", "scope_required"), (409, "Conflict", "something_unheard_of")],
)
def test_a_caller_can_keep_the_http_title_alone(status: int, title: str, code: str) -> None:
    """The trigger banners: a code never goes in a title (the lead's ruling, 01a11bf7-15b7), so they ask the reader not to append it."""
    env = {"type": "/errors/x", "title": title, "status": status, "detail": code, "extensions": {"code": code}}

    got = _read(_context(), env, None, {"codeAfterTitle": False})

    assert got["code"] == code and got["sentence"] == "" and got["message"] == title


def test_a_request_validation_error_has_no_code_to_print(envelopes) -> None:
    """The admin banners print ``Create failed (<code>)``: for a field error the ``type`` is a pydantic type (``string_too_short``) or a check's name, not a code."""
    for key in ("validated", "agent_field"):
        assert _read(_context(), envelopes[key])["code"] is None, key


def test_exactly_one_validation_error_is_said_in_its_own_words_with_its_field_in_front_for_a_generic_toast() -> None:
    """"String should have at least 1 character" alone does not say which field (review of #625, round 2): the message names it, the sentence stays the server's msg."""
    env = {
        "type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "One or more request parameters or body fields failed validation.",
        "extensions": {"errors": [{"type": "missing", "loc": ["body", "name"], "msg": "Field required"}]},
    }

    assert _read(_context(), env) == {"code": None, "field": "name", "sentence": "Field required", "message": "name: Field required"}


def test_a_nested_field_is_named_by_its_dotted_path() -> None:
    env = {
        "type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "failed",
        "extensions": {"errors": [{"type": "string_too_short", "loc": ["body", "model", "profile_id"], "msg": "String should have at least 1 character"}]},
    }

    got = _read(_context(), env)

    assert got["field"] == "model.profile_id" and got["sentence"] == "String should have at least 1 character"
    assert got["message"] == "model.profile_id: String should have at least 1 character"


def test_a_single_validation_error_that_names_no_field_is_its_msg_alone() -> None:
    env = {
        "type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "failed",
        "extensions": {"errors": [{"type": "value_error", "msg": "Value error, the two passwords differ"}]},
    }

    assert _read(_context(), env) == {"code": None, "field": None, "sentence": "Value error, the two passwords differ", "message": "Value error, the two passwords differ"}


def test_several_validation_errors_are_not_said_as_one_of_them(envelopes) -> None:
    got = _read(_context(), envelopes["validated"])

    assert got["sentence"] == "" and got["message"] == "Missing or invalid: name, count."


@pytest.mark.parametrize("errors", [[None], [42], [{}], [{"loc": "body"}], [{"loc": []}], [{"msg": 7}]])
def test_a_malformed_validation_entry_does_not_break_the_reader(errors) -> None:
    """The reader runs inside every error handler: a surprising entry must give an answer, never a TypeError."""
    err = {"title": "Validation Error", "detail": "failed", "message": "failed", "envelope": {"extensions": {"errors": errors}}}

    got = json.loads(_context().eval(f"JSON.stringify(window.primerApi.readRefusal({json.dumps(err)}))"))

    assert got["code"] is None and got["field"] is None and got["message"] == "failed", got


@pytest.mark.parametrize(
    ("loc", "field", "message", "said"),
    [
        (["body"], "body", "Field required", "Field required"),                  # the whole body is missing or not an object: "body: Field required" names nothing
        (["body", 1], "1", "JSON decode error", "JSON decode error"),            # FastAPI's character position in a body that is not JSON: "1: JSON decode error"
        (["query"], "query", "Field required", "Field required"),
        (["body", 0], "0", "Input should be a valid string", "Input should be a valid string"),   # two long, ending in a number: a position, as above
        # a list item of a named field IS a field the person can find: the first version of the skip dropped it (a scalar list item lost its field vs main)
        (["body", "items", 2], "items.2", "Input should be a valid string", "items.2: Input should be a valid string"),
        (["body", "members", 0], "members.0", "Input should be a valid string", "members.0: Input should be a valid string"),
        (["query", "ids", 3], "ids.3", "Input should be a valid integer", "ids.3: Input should be a valid integer"),
        (["body", "items", 2, "name"], "items.2.name", "Field required", "items.2.name: Field required"),
    ],
)
def test_the_field_goes_in_front_of_a_message_only_when_it_names_a_field(loc: list, field: str, message: str, said: str) -> None:
    env = {
        "type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "failed",
        "extensions": {"errors": [{"type": "x", "loc": loc, "msg": message}]},
    }

    got = _read(_context(), env)

    assert got["sentence"] == message and got["field"] == field
    assert got["message"] == said, got
