"""ONE reader for a refused write (ticket 01a11cd1-7aaf), and the two refusals it makes readable (admin review ADM-12 and ADM-20).

Five components each read a failed write out of the RFC 7807 envelope their own way (``MC_extractError``, ``AT_extractError``, ``ADM_extractError``,
``SSO_extractError`` and ``TR_refusal``), and they disagreed: the trigger reader knew the auth gate puts its code in ``extensions.error`` and sends no
message, the others printed the gate's bare ``auth_required`` as the message, and ``SSO_extractError`` still looked for the code in a ``detail`` object
that the handler never sends (so its code was always null). ``window.primerApi.readRefusal`` (``ui/foundation/api.js``, next to ``ApiError``) is the one
reader: ``{code, field, sentence, message}``.

* ``code``: ``extensions.code`` (routers raise ``HTTPException(detail={code, message})``), ``extensions.error`` (the auth gate, and the agent/profile pre-write
  422s ``{error, field, message}``), the first ``extensions.errors[]`` type (request validation), ``in_use_by`` for a reference block, an older ``detail`` object,
  or a ``detail`` that is only a snake_case code.
* ``field``: ``extensions.field`` or the dotted path of the first request-validation error (``body.`` dropped).
* ``sentence``: the server's own words, or ``""`` when it sent none or only the code. A message equal to the code is not a sentence; one that merely looks like a
  code is kept when the code is known and different (the not-found message is a bare id, and an id may hold an underscore).
* ``message``: what a person reads. The sentence, except that a reference block (``in_use_by: 1 agent(s) reference 'x' (first: 'y')``) and a duplicate
  (``Channel with provider_id='a', external_id='b' already exists (id='c')``) are said in plain words (ADM-12, ADM-20); with no sentence, a known bare code
  (``auth_required``, ``forbidden_role``) gets its own sentence, else the HTTP title, then the error's message, then the caller's fallback.

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


def _read(ctx, envelope: dict, fallback: str | None = None) -> dict:
    arg = json.dumps(fallback)
    return json.loads(ctx.eval(f"JSON.stringify(window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelope)}), {arg}))"))


# ---- the reader, on every shape the API really answers ----------------------------------------------------------------------------------------------------

_IN_USE = "'llm-openchat--scripted:default' is still in use by an agent, for example builder. Remove or change that first."
_IN_USE_SESSION = "'ag-1' is still in use by a session, for example sess-0001. Remove or change that first."
_CHANNEL = "A channel with provider_id rev-slack and external_id C0AAAA0001 already exists: channel-fbf469c47c2a."

EXPECTED = {
    "session_ended": {"code": "auth_required", "field": None, "sentence": "", "message": "Your session has ended; sign in again."},
    "role_refused": {"code": "forbidden_role", "field": None, "sentence": "", "message": "Your role does not allow this."},
    "router_code": {
        "code": "trigger_slug_conflict", "field": None, "sentence": "slug 'nightly' already in use", "message": "slug 'nightly' already in use",
    },
    "router_code_bare_id": {"code": "trigger_not_found", "field": None, "sentence": "nightly_job", "message": "nightly_job"},
    "pre_write": {
        "code": "profile_not_found", "field": "model.profile_id", "sentence": "profile 'p-9' does not exist", "message": "profile 'p-9' does not exist",
    },
    "agent_field": {"code": "agent_id_invalid", "field": "id", "sentence": "", "message": "Missing or invalid: id."},
    "validated": {"code": "string_too_short", "field": "name", "sentence": "", "message": "Missing or invalid: name, count."},
    "in_use_by": {
        "code": "in_use_by", "field": None,
        "sentence": "in_use_by: 1 agent(s) reference 'llm-openchat--scripted:default' (first: 'builder')", "message": _IN_USE,
    },
    "in_use_by_session": {
        "code": "in_use_by", "field": None, "sentence": "in_use_by: 1 session(s) reference 'ag-1' (first: 'sess-0001')", "message": _IN_USE_SESSION,
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
    assert field["extensions"]["errors"][0]["type"] == "agent_id_invalid" and field["extensions"]["errors"][0]["loc"] == ["body", "id"]
    assert pre["extensions"]["error"] == "profile_not_found" and pre["extensions"]["field"] == "model.profile_id"
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

    assert _read(_context(), env) == {"code": "something_unheard_of", "field": None, "sentence": "", "message": "Conflict"}


def test_a_detail_that_is_only_a_snake_case_code_is_the_code() -> None:
    env = {"type": "/errors/bad-request", "title": "Bad Request", "status": 400, "detail": "payload_malformed"}

    assert _read(_context(), env) == {"code": "payload_malformed", "field": None, "sentence": "", "message": "Bad Request"}


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


# ---- the five readers are the one reader -----------------------------------------------------------------------------------------------------------------

_EXTRACTORS = [
    ("components/mcp.jsx", "MC_extractError"),
    ("components/api_tokens.jsx", "AT_extractError"),
    ("components/admin_users.jsx", "ADM_extractError"),
    ("components/sso_admin.jsx", "SSO_extractError"),
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


def test_the_foundation_reader_is_the_only_place_that_reads_a_refusal_out_of_an_envelope() -> None:
    """A sixth copy is how this started. A component may read ``.envelope.extensions`` only to pick ONE field the reader does not return: the Python editor's
    registration message (the line and function are the reason it is shown inline)."""
    allowed = {"components/toolsets/python-editor.jsx"}
    offenders = []
    for path in sorted((UI / "components").rglob("*.jsx")):
        rel = path.relative_to(UI).as_posix()
        if rel in allowed or rel.endswith("mock-data.jsx"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]
            if re.search(r"\b(?:ext|extensions|env\.extensions|envelope\.extensions)\.(?:code|error|message|field)\b", code):
                offenders.append(f"{rel}:{number}: {line.strip()[:110]}")
    assert not offenders, "a component reads a refusal out of the envelope itself instead of window.primerApi.readRefusal:\n" + "\n".join(offenders)


_CHANNELS_PRELUDE = """
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
globalThis.Btn = function (props) { return React.createElement("button", { "data-testid": "btn-" + props.kind, onClick: props.onClick, disabled: props.disabled }, props.children); };
globalThis.Icon = function () { return null; };
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


def test_the_default_error_toast_says_the_refusal_in_plain_words(envelopes) -> None:
    """ADM-20: the toast a refused channel create raised read ``Channel with provider_id='rev-slack', external_id='C0AAAA0001' already exists (id='channel-...')``."""
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
        var mutation = window.primerApi.useMutation(function () {{ throw new window.primerApi.ApiError({json.dumps(envelopes["channel_conflict"])}); }});
        mutation.mutate({{}}).catch(function () {{}});
        """
    )
    ctx.eval("0")  # let the promise continuation run
    pushed = json.loads(ctx.eval("JSON.stringify(pushed)"))

    assert pushed and pushed[0]["detail"] == _CHANNEL, pushed
