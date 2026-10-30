"""ACL-P1-T10: the console Share dialog for agents, graphs, toolsets,
workspace templates and channels (plan docs/superpowers/plans/2026-10-30-
access-control-p1-foundation.md, Task 10; spec sections 6.2 and 9).

Owned entities get owners, read/full grants and a public switch, enforced
server-side (the access sub-resource, Tasks 6-7). This file pins the
CONSOLE side against the contract:

* ``SH_api`` gains the only URL builders for the contract:
  ``GET /{plural}/{id}/access`` (read), ``PUT`` with the loaded ``rev``
  (write), and ``GET /users/lookup?q=<prefix>&limit=10``.
* ``ui/components/shared/share-dialog.jsx`` (``SHR_*`` globals) loads
  right after the platform kit, draws the access view (loading is
  ``role=status``, a failed read is ``role=alert`` + Retry), saves through
  ``useMutation`` with the rev, shows "Sharing changed since you opened
  this; reload" on 409 with a Reload action, shows 422 field errors
  inline, and is read-only for a ``your_level`` that is not ``owner``.
* the verb ``entity.share`` registers in ``nv-shell.jsx`` with a
  pointer surface (the registry rejects palette-only verbs) and a label
  the registry's lint accepts; the open owned-entity surface subscribes
  (``SHR_onShare``) and the verb's run dispatches to it.
* a Share button (``data-verb="entity.share"``) sits in the five detail
  surfaces, and the five list surfaces draw Shared/Public badges from
  the row's ``access`` field.

The URL builders and the dialog run here in MiniRacer against the real
source (the house style of tests/ui/test_nv_g14_*.py and the
``_mini_react.py`` harness); the JSX wiring in the five surfaces and the
verb registration are source checks, anchored on the real lines.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

INDEX = (UI / "index.html").read_text(encoding="utf-8")
SH_API_SRC = UI / "components" / "shell" / "sh-api.jsx"
SHELL_VERBS = (UI / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")
NV_SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
AGENTS = (UI / "components" / "agents.jsx").read_text(encoding="utf-8")
GRAPHS = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")
TOOLSETS = (UI / "components" / "toolsets.jsx").read_text(encoding="utf-8")
TEMPLATES = (UI / "components" / "workspaces" / "templates.jsx").read_text(encoding="utf-8")
CHANNELS = (UI / "components" / "channels.jsx").read_text(encoding="utf-8")
DIALOG_SRC = UI / "components" / "shared" / "share-dialog.jsx"

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _transpiled(path: Path) -> str:
    from ui._mini_react import transpile

    return transpile(path)


# ---- the bundle order -------------------------------------------------------


def test_the_dialog_script_loads_right_after_the_platform_kit() -> None:
    """share-dialog.jsx builds on the kit's neighbours (Modal/Btn from
    shared.jsx, PK_* from platform-kit.jsx), so it loads directly after
    the kit and before every page component (ui/index.html discipline)."""
    kit = 'src="components/shared/platform-kit.jsx"'
    tag = 'src="components/shared/share-dialog.jsx"'
    assert kit in INDEX, "the platform kit tag moved"
    assert tag in INDEX, "the share-dialog script tag is missing from ui/index.html"
    kit_end = INDEX.index(kit) + len(kit)
    tag_start = INDEX.rindex("<script", 0, INDEX.index(tag))
    assert tag_start > kit_end, "share-dialog.jsx must load after platform-kit.jsx"
    assert "<script" not in INDEX[kit_end:tag_start], (
        "another script landed between the platform kit and the share dialog"
    )


# ---- SH_api: the only URL builder for the contract ---------------------------

_SH_API_PRELUDE = """
var __calls = [];
window.primerApi = { apiFetch: function (m, p, b, o) { __calls.push([m, p, b, o]); return { then: function (f) { return { catch: function () {} }; } }; } };
var PR_fetchAllPages = function (path, opts) { __calls.push(["pages", path, opts]); return { then: function (f) { return { catch: function () {} }; } }; };
"""


def _sh_api_ctx():
    from ui._mini_react import mini_react_context

    ctx = mini_react_context(_transpiled(SH_API_SRC), prelude=_SH_API_PRELUDE)
    _OPEN_CONTEXTS.append(ctx)
    return ctx


def _last_call(ctx):
    return json.loads(ctx.eval("JSON.stringify(__calls)"))[-1]


def test_get_access_builds_the_contract_read() -> None:
    """GET /v1/{plural}/{id}/access with the read's AbortSignal
    (every read in sh-api.jsx takes one, so useResource can cancel it)."""
    ctx = _sh_api_ctx()
    ctx.eval('SH_api.getAccess("agents", "a-1", "SIG")')
    call = _last_call(ctx)
    assert call[0] == "GET"
    assert call[1] == "/agents/a-1/access"
    assert call[3] == {"signal": "SIG"}


def test_get_access_covers_the_five_plurals_and_encodes_the_id() -> None:
    """The plural is an argument, never a hardcode: the same builder
    serves workspace_templates (the console's long name) and encodes an
    id the way every other sh-api read does."""
    ctx = _sh_api_ctx()
    for plural in ("agents", "graphs", "toolsets", "workspace_templates", "channels"):
        ctx.eval('SH_api.getAccess("' + plural + '", "x-1", "SIG")')
        assert _last_call(ctx)[1] == "/" + plural + "/x-1/access"
    ctx.eval('SH_api.getAccess("workspace_templates", "wt/1", "SIG")')
    assert _last_call(ctx)[1] == "/workspace_templates/wt%2F1/access"


def test_set_access_puts_the_edit_body_at_the_same_path() -> None:
    """PUT /v1/{plural}/{id}/access: the body the dialog builds (owners,
    grants, public, public_tools, rev) goes out verbatim - SH_api does
    not add, drop or rename a field."""
    ctx = _sh_api_ctx()
    body = {"owners": ["u-1"], "grants": {"u-2": "read"}, "public": True, "public_tools": [], "rev": 7}
    ctx.eval("SH_api.setAccess(\"toolsets\", \"t-1\", " + json.dumps(body) + ")")
    call = _last_call(ctx)
    assert call[0] == "PUT"
    assert call[1] == "/toolsets/t-1/access"
    assert call[2] == body


def test_lookup_users_builds_the_prefix_query() -> None:
    """GET /v1/users/lookup?q=<prefix>&limit=10: the dialog only ever
    needs the top ten prefix matches."""
    ctx = _sh_api_ctx()
    ctx.eval('SH_api.lookupUsers("bo", "SIG")')
    call = _last_call(ctx)
    assert call[0] == "GET"
    assert call[1] == "/users/lookup?q=bo&limit=10"
    assert call[3] == {"signal": "SIG"}


# ---- the verb rules: "Share Entity" registers, palette-only does not --------


def test_share_opens_a_verb_label_and_the_entity_share_shape_registers() -> None:
    """The registry's lint (shell-verbs.js) only lets a known verb word
    open a Title Case two-plus-word label, and it rejects a verb with no
    pointer surface. The real rules must accept the exact shape
    nv-shell.jsx registers entity.share with."""
    from ui._mini_react import mini_react_context

    ctx = mini_react_context(SHELL_VERBS)
    _OPEN_CONTEXTS.append(ctx)
    assert ctx.eval('SH_VERB_WORDS.indexOf("Share") >= 0'), (
        '"Share" is not in SH_VERB_WORDS, so no entity.share label can pass the lint'
    )
    assert ctx.eval('SH_lintVerbLabel("Share Entity")') is None
    ctx.eval(
        "SH_createVerbRegistry().register({ id: 'entity.share', label: 'Share Entity', "
        "surfaces: ['overlay-button', 'palette'], run: function () {} })"
    )
    with pytest.raises(Exception):
        ctx.eval(
            "SH_createVerbRegistry().register({ id: 'x.share', label: 'Share Entity', "
            "surfaces: ['palette'], run: function () {} })"
        )


def test_nv_shell_registers_entity_share_with_a_pointer_surface_and_a_gate() -> None:
    """The verb registers with the overlay-button surface (the Share
    button the detail surfaces draw) AND the palette, and its available
    gate reads the open overlay so the palette does not offer sharing
    with nothing shareable open."""
    assert "entity.share" in NV_SHELL, "entity.share is not registered in nv-shell.jsx"
    anchor = 'id: "entity.share"'
    assert anchor in NV_SHELL
    start = NV_SHELL.index(anchor)
    block = NV_SHELL[start: start + 500]
    assert 'surfaces: ["overlay-button", "palette"]' in block, (
        "entity.share must declare the overlay-button pointer surface and the palette"
    )
    assert "available" in block, "entity.share has no availability gate"
    assert "SHR_requestShare" in block, "entity.share's run() does not dispatch to the share seam"


# ---- the five surfaces: the button, the subscription, the badges ------------


def test_every_detail_surface_offers_the_share_verb_and_mounts_the_dialog() -> None:
    for name, src, button in (
        ("agents.jsx", AGENTS, "agent-share"),
        ("graphs.jsx", GRAPHS, "graph-share"),
        ("toolsets.jsx", TOOLSETS, "toolset-share"),
        ("workspaces/templates.jsx", TEMPLATES, "wt-share"),
        ("channels.jsx", CHANNELS, "channel-share"),
    ):
        assert f'data-testid="{button}"' in src, name + " has no Share button"
        assert 'data-verb="entity.share"' in src, (
            name + " 's Share button does not carry the verb id (dual-render rule)"
        )
        assert "SHR_ShareDialog" in src, name + " does not mount the share dialog"
        assert "SHR_useShareOpen" in src, (
            name + " does not subscribe to the share seam (the palette verb would be a no-op)"
        )


def test_the_channel_share_button_only_offers_it_in_edit_mode() -> None:
    """Channels have no per-channel detail overlay: the Share button
    lives in the channel form's edit mode, so a create must not offer
    sharing a channel that has no row yet."""
    i = CHANNELS.index('data-testid="channel-share"')
    assert "isEdit" in CHANNELS[max(0, i - 250):i], (
        "the channel Share button is not gated on edit mode"
    )


def test_the_platform_lists_draw_access_badges_from_the_row() -> None:
    """Every list row carries the row's access (the owned CRUD shape),
    so a list says at a glance which rows are shared or public without
    opening them."""
    for name, src, needle in (
        ("agents.jsx", AGENTS, "SHR_accessBadges(a.access)"),
        ("graphs.jsx", GRAPHS, "SHR_accessBadges(g.access)"),
        ("toolsets.jsx", TOOLSETS, "SHR_accessBadges(t.access)"),
        ("channels.jsx", CHANNELS, "SHR_accessBadges(c.access)"),
        ("workspaces/templates.jsx", TEMPLATES, "SHR_accessBadges(t.access)"),
    ):
        assert needle in src, name + " does not draw its access badges from the row"


# ---- the dialog: the real file in MiniRacer ----------------------------------

# The dialog's globals, stubbed the way the harness documents (a stub that
# does something else, or nothing, tests the stub): Modal/Btn/FormField are
# shared.jsx components tested elsewhere, here they just pass props through
# so the test reaches the dialog's own testids, roles and callbacks.
DIALOG_PRELUDE = r"""
var __calls = [];
window.__calls = __calls;
var __resState = { data: undefined, error: null, loading: false };
var __refetches = 0;
var __puts = [];
var __putResults = [];
var __lookups = [];
function thenOk(v) { return { then: function (f, r) { if (f) f(v); return thenOk(v); }, catch: function () { return this; } }; }
function thenErr(e) { return { then: function (f, r) { if (r) r(e); return thenErr(e); }, catch: function (f) { if (f) f(e); return thenOk(undefined); } }; }
window.primerApi = {
  useResource: function (key, fetcher, opts) {
    __resKey = key;
    return {
      data: __resState.data, error: __resState.error, loading: __resState.loading,
      degraded: false,
      refetch: function () { __refetches++; },
    };
  },
  useMutation: function (fetcher, opts) {
    __mutFetch = fetcher;
    return {
      mutate: function (body) {
        var p = fetcher(body);
        p.then(
          function (data) { if (opts && typeof opts.onSuccess === "function") opts.onSuccess(data); },
          function (err) { if (opts && typeof opts.onError === "function") opts.onError(err); }
        );
        return p;
      },
      loading: false, error: null, data: undefined,
    };
  },
  readRefusal: function (err) { return { message: (err && (err.detail || err.message)) || "refused" }; },
  toastPush: function (t) { __calls.push(["toast", t.kind, t.title, t.detail]); },
  useViewport: function () { return { isMobile: false }; },
};
var SH_api = {
  getAccess: function (plural, id, signal) { return thenOk(__resState.data); },
  // The response queue lives at the API edge: the fetcher the dialog hands
  // useMutation is SH_api.setAccess itself, so a queued refusal surfaces
  // the way the server's 409/422 would.
  setAccess: function (plural, id, body) {
    __puts.push([plural, id, body]);
    var out = __putResults.length ? __putResults.shift() : { ok: true, value: body };
    return out.ok ? thenOk(out.value) : thenErr(out.error);
  },
  lookupUsers: function (q, signal) {
    __lookups.push(q);
    return thenOk([{ id: "u-2", username: "bob" }, { id: "u-3", username: "bobby" }]);
  },
};
var Modal = function (props) {
  var kids = [props.children];
  if (props.footer) kids.push(React.createElement("div", { "data-testid": "stub-footer" }, props.footer));
  return React.createElement("div", { "data-testid": props.testid, role: "dialog" }, kids[0], kids[1]);
};
var Btn = function (props) {
  return React.createElement("button", {
    "data-testid": props["data-testid"],
    disabled: props.disabled,
    "aria-busy": props.busy ? "true" : undefined,
    onClick: props.onClick,
  }, props.children);
};
"""

OWNER_VIEW = {
    "owners": [{"id": "u-1", "username": "amy"}],
    "grants": [
        {"user": {"id": "u-2", "username": "bob"}, "level": "read"},
        {"user": {"id": "u-3", "username": "cara"}, "level": "full"},
    ],
    "public": True,
    "public_tools": ["web.search"],
    "rev": 7,
    "your_level": "owner",
}

MOUNT = (
    'MR.mount(SHR_ShareDialog, { plural: "agents", id: "a-1", title: "Agent a-1", '
    "onClose: function () {} })"
)
MOUNT_TOOLSET = (
    'MR.mount(SHR_ShareDialog, { plural: "toolsets", id: "t-1", title: "Toolset t-1", '
    'allowPublicTools: true, tools: ["web.search", "fs.read"], onClose: function () {} })'
)


def _dialog_ctx(view: dict | None = None):
    from ui._mini_react import mini_react_context

    prelude = DIALOG_PRELUDE + "var __resKey = null; var __mutFetch = null;"
    if view is not None:
        prelude += "__resState = { data: " + json.dumps(view) + ", error: null, loading: false };"
    ctx = mini_react_context(_transpiled(DIALOG_SRC), prelude=prelude)
    _OPEN_CONTEXTS.append(ctx)
    return ctx


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_the_loading_state_is_a_status() -> None:
    """While the access read is in flight the dialog says it is loading,
    as a screen-reader status, not a confident empty view."""
    ctx = _dialog_ctx()
    ctx.eval("__resState = { data: undefined, error: null, loading: true };")
    ctx.eval(MOUNT)
    assert ctx.eval('MR.find("shr-dialog") ? 1 : 0') == 1, "the dialog is not open"
    sub = json.loads(ctx.eval('JSON.stringify(MR.subtree("shr-loading"))'))
    assert sub and sub[0].get("role") == "status"


def test_a_failed_read_says_why_and_offers_retry() -> None:
    """A 404 access read is the dialog's error state: the refusal's
    words (readRefusal) plus a Retry that refires the read."""
    ctx = _dialog_ctx()
    ctx.eval(
        "__resState = { data: undefined, error: { title: 'Not found', status: 404, "
        "detail: 'No such agent' }, loading: false };"
    )
    ctx.eval(MOUNT)
    sub = json.loads(ctx.eval('JSON.stringify(MR.subtree("shr-error"))'))
    assert sub and sub[0].get("role") == "alert"
    assert "No such agent" in _texts(ctx), "the refusal's words are missing from the error state"
    ctx.eval('MR.click("shr-retry")')
    assert ctx.eval("__refetches") == 1, "Retry does not refire the access read"


def test_the_view_lists_owners_grants_and_the_public_switch() -> None:
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(MOUNT)
    texts = _texts(ctx)
    assert "amy" in texts and "bob" in texts and "cara" in texts
    levels = json.loads(
        ctx.eval(
            'JSON.stringify(MR.findAll("shr-grant-level-").map(function (e) { '
            "return [e.props['data-testid'], e.props.value]; }))"
        )
    )
    assert sorted(levels) == [
        ["shr-grant-level-u-2", "read"],
        ["shr-grant-level-u-3", "full"],
    ]
    assert ctx.eval('__resKey') == "shr-access:agents:a-1"
    assert ctx.eval('MR.find("shr-public") ? MR.find("shr-public").props.checked === true : false')


def test_a_non_owner_gets_the_read_only_dialog() -> None:
    """your_level 'read' (or 'full') sees who has access but cannot
    change it: no Save, no controls, one line saying owners only."""
    view = dict(OWNER_VIEW, your_level="read")
    ctx = _dialog_ctx(view)
    ctx.eval(MOUNT)
    assert ctx.eval('MR.find("shr-readonly") ? 1 : 0') == 1
    assert "Only owners can change sharing" in _texts(ctx)
    assert ctx.eval('MR.find("shr-save") ? 1 : 0') == 0, "a non-owner is offered a Save"
    assert ctx.eval('MR.find("shr-add-user") ? 1 : 0') == 0


def test_save_posts_the_contract_body_with_the_loaded_rev() -> None:
    """The save PUTs exactly the contract body: the owners as ids, the
    grants as a {user id: level} map, the public switch, public_tools,
    and the rev the view loaded with (the 409 guard the server checks)."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(MOUNT)
    ctx.eval('MR.findProp("data-testid", "shr-grant-level-u-2").props.onChange({ target: { value: "full" } })')
    ctx.eval("MR.rerender()")
    ctx.eval('MR.click("shr-save")')
    puts = json.loads(ctx.eval("JSON.stringify(__puts)"))
    assert puts and puts[-1][0] == "agents" and puts[-1][1] == "a-1"
    assert puts[-1][2] == {
        "owners": ["u-1"],
        "grants": {"u-2": "full", "u-3": "full"},
        "public": True,
        "public_tools": ["web.search"],
        "rev": 7,
    }


def test_a_stale_save_says_reload_and_the_reload_clears_it() -> None:
    """409 is the server's 'someone else changed sharing while you were
    editing' signal: the dialog must say so verbatim and offer a Reload
    that refetches (which also resets the draft onto the fresh view)."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(MOUNT)
    ctx.eval('__putResults.push({ ok: false, error: { status: 409, title: "Conflict" } })')
    ctx.eval('MR.click("shr-save")')
    sub = json.loads(ctx.eval('JSON.stringify(MR.subtree("shr-stale"))'))
    assert sub and sub[0].get("role") == "alert"
    assert "Sharing changed since you opened this; reload" in _texts(ctx)
    ctx.eval('MR.click("shr-stale-reload")')
    assert ctx.eval("__refetches") >= 1
    ctx.eval(
        "__resState.data = " + json.dumps(dict(OWNER_VIEW, rev=8)) + "; MR.rerender();"
    )
    assert ctx.eval('MR.find("shr-stale") ? 1 : 0') == 0, "the stale banner survived the reload"


def test_a_422_shows_the_field_error_inline() -> None:
    """422 is the server's 'this edit is invalid' signal (empty owners,
    unknown user, public_tools on a non-toolset): the dialog shows the
    field message inline, not a toast, and keeps the draft."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(MOUNT)
    ctx.eval(
        '__putResults.push({ ok: false, error: { status: 422, fieldErrors: '
        '[{ loc: ["owners"], msg: "At least one owner is required" }] } })'
    )
    ctx.eval('MR.click("shr-save")')
    sub = json.loads(ctx.eval('JSON.stringify(MR.subtree("shr-field-errors"))'))
    assert sub and sub[0].get("role") == "alert"
    assert "At least one owner is required" in _texts(ctx)
    assert ctx.eval("__puts.length") == 1, "the invalid save was not retried"


def test_an_unexpected_refusal_toasts_instead_of_silencing() -> None:
    """A refusal the dialog does not own (a network 500, a 403) still
    reaches the user: since the dialog supplies its own onError, it must
    push the refusal's words to the toast host itself."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(MOUNT)
    ctx.eval(
        '__putResults.push({ ok: false, error: { status: 500, title: "Internal error", '
        'detail: "boom" } })'
    )
    ctx.eval('MR.click("shr-save")')
    toasts = json.loads(ctx.eval("JSON.stringify(__calls)"))
    assert any(t[0] == "toast" and t[1] == "error" for t in toasts), (
        "the unhandled refusal never reached the toast host: " + json.dumps(toasts)
    )


def test_the_toolset_dialog_toggles_each_tools_public_flag() -> None:
    """public_tools is a toolset-only field (the server 422s it on the
    other four plurals): only the toolset dialog offers the per-tool
    switches, and the save body carries the toggled names."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(MOUNT_TOOLSET)
    tools = json.loads(ctx.eval('JSON.stringify(MR.findAll("shr-public-tool-"))'))
    assert len(tools) == 2
    ctx.eval('MR.findProp("data-testid", "shr-public-tool-fs.read").props.onChange({ target: { checked: true } })')
    ctx.eval("MR.rerender()")
    ctx.eval('MR.click("shr-save")')
    body = json.loads(ctx.eval("JSON.stringify(__puts)"))[-1][2]
    assert sorted(body["public_tools"]) == ["fs.read", "web.search"]
    # the non-toolset dialog offers no per-tool switches at all
    ctx2 = _dialog_ctx(OWNER_VIEW)
    ctx2.eval(MOUNT)
    assert ctx2.eval('MR.findAll("shr-public-tool-").length') == 0


def test_adding_a_person_uses_the_lookup_and_defaults_to_read() -> None:
    """Granting starts from a username prefix: the lookup (top ten)
    offers the matches, picking one adds a read grant (least privilege),
    and the dialog's own lookup URL never leaves SH_api."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(MOUNT)
    ctx.eval('MR.findProp("data-testid", "shr-add-user").props.onChange({ target: { value: "bo" } })')
    ctx.eval("MR.rerender()")
    assert json.loads(ctx.eval("JSON.stringify(__lookups)")) == ["bo"]
    assert ctx.eval('MR.find("shr-lookup-u-2") ? 1 : 0') == 1, "the lookup matches are not offered"
    assert "bob" in _texts(ctx)
    ctx.eval('MR.click("shr-lookup-u-2")')
    ctx.eval("MR.rerender()")
    assert ctx.eval('MR.findProp("data-testid", "shr-grant-level-u-2") ? 1 : 0') == 1
    assert ctx.eval('MR.findProp("data-testid", "shr-grant-level-u-2").props.value') == "read"


# ---- the share seam the verb dispatches through ------------------------------


def test_the_share_seam_dispatches_to_the_open_surface_and_only_it() -> None:
    """SHR_onShare/SHR_requestShare is the one slot between the verb
    (nv-shell.jsx) and the open owned-entity surface: the surface that
    is open opens the dialog, and a dispatch with no subscriber (a
    palette run with nothing shareable open) is a no-op, not a throw."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval("var __hits = 0; var __off = SHR_onShare(function () { __hits++; });")
    ctx.eval("SHR_requestShare();")
    assert ctx.eval("__hits") == 1
    assert ctx.eval("SHR_shareLive() === true")
    ctx.eval("__off(); SHR_requestShare();")
    assert ctx.eval("__hits") == 1, "a dispatch with no subscriber did nothing to the world"
    assert ctx.eval("SHR_shareLive() === false")


def test_shr_use_share_open_opens_when_the_verb_dispatches() -> None:
    """The hook the five surfaces use: subscribed while the surface is
    mounted, it flips open on a dispatch (the palette's 'Share Entity')
    and stays open until the dialog closes."""
    ctx = _dialog_ctx(OWNER_VIEW)
    ctx.eval(
        "MR.mount(function () { var r = SHR_useShareOpen(); "
        'return React.createElement("i", { "data-testid": "probe", open: r[0] }); }, {});'
    )
    assert ctx.eval('MR.find("probe") ? 1 : 0') == 1, "the subscribing surface is mounted"
    ctx.eval("SHR_requestShare(); MR.rerender();")
    assert ctx.eval('MR.find("probe").props.open === true')
