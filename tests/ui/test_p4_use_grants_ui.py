"""ACL-P4-T7: the console side of use grants on grantable infrastructure (plan
docs/superpowers/plans/2026-10-31-access-control-p4-runs-infrastructure.md,
Task 7).

The backend (Tasks 2-3, already merged onto this branch) serves the access
sub-resource for the seven grantable kinds (``infra_access.py``: model
profiles, LLM / embedding / cross-encoder providers, semantic search,
workspace and channel providers) with ``use_only``: no owners, ``read``
grants (meaning *use*) and a public switch, PUT admin-only. Non-admins get
only ``your_level`` and ``public``. This file pins the CONSOLE side:

* ``share-dialog.jsx`` gains the ``useOnly`` mode of the Share dialog (no
  owners, no level picker: "People who can use" + "Everyone can use";
  admins edit, everyone else reads-only), the card badge of a grantable
  row (``SHR_useBadge``: "Public" / "N people" / "Admins only") and
  ``SHR_makePublic`` (the create form's "Public" switch cannot ride the
  POST - the server stamps a private ``Access`` and ignores the body - so
  the switch follows the POST with the access GET + PUT).
* The detail overlays (model profiles, workspace providers, channel
  providers, semantic search, the register overlay's edit mode for LLM /
  embedding / cross-encoder) offer the "Who can use" button and the
  ``entity.share`` verb; the list cards badge the row's access.
* The create forms of those kinds offer a "Public" switch (default off),
  which makes the new row public through the access PUT.
* A current model profile that is not in ``/model_profiles/choices`` is
  said "(not available to you)" in the agent form and the graph node's
  profile picker, with a warning.
* A PAUSED session whose ``last_turn_error.code`` is
  ``model_profile_unusable`` says its line (the ERROR record renders its
  own message).
* The tool picker needs no change: the public tools of unreadable
  toolsets are served already filtered (``GET /v1/tools``).

The dialog, the badge, the access PUT and the paused line run in MiniRacer
against the real source; the JSX wiring of the overlays and the create
forms is pinned on the real lines (the house pattern of
test_share_dialog.py).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

SHARE_SRC = UI / "components" / "shared" / "share-dialog.jsx"
MODEL_PROFILES = (UI / "components" / "model-profiles.jsx").read_text(encoding="utf-8")
WP_PROVIDERS = (UI / "components" / "workspaces" / "providers.jsx").read_text(encoding="utf-8")
CHANNELS = (UI / "components" / "channels.jsx").read_text(encoding="utf-8")
SEMANTIC = (UI / "components" / "semantic-search.jsx").read_text(encoding="utf-8")
CATALOG = (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
REG_LLM = (UI / "components" / "providers" / "register-llm.jsx").read_text(encoding="utf-8")
REG_MODELS = (UI / "components" / "providers" / "register-models.jsx").read_text(encoding="utf-8")
AGENTS = (UI / "components" / "agents.jsx").read_text(encoding="utf-8")
SESSION_DOC = (UI / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
GB_INSPECTOR = (UI / "components" / "graph-builder" / "gb-inspector.jsx").read_text(encoding="utf-8")

EXACT_TEXT = (
    "This turn did not run: its model profile is not available to the person who sent it."
    " An admin can grant its use, then Resume."
)


def _transpiled(path: Path) -> str:
    from tests.ui._mini_react import transpile

    return transpile(path)


# ---- the card badge ---------------------------------------------------------


def test_the_use_badge_names_the_three_states() -> None:
    """Public when anyone can use it, "N people" when a use grant exists,
    else "Admins only" (new rows default private and the CRUD is admin
    tier): a row without an access field says nothing."""
    from tests.ui._mini_react import mini_react_context

    ctx = mini_react_context(_transpiled(SHARE_SRC))
    try:
        got = json.loads(
            ctx.eval(
                "JSON.stringify(["
                "SHR_useBadge({ public: true, grants: [] }),"
                "SHR_useBadge({ public: false, grants: [ { user: { id: 'u-1' } }, { user: { id: 'u-2' } } ] }),"
                "SHR_useBadge({ public: false, grants: [] }),"
                "SHR_useBadge(null)"
                "])"
            )
        )
    finally:
        ctx.close()
    assert got == ["Public", "2 people", "Admins only", None]


# ---- the create form's follow-up access PUT ---------------------------------


_MAKE_PUBLIC_PRELUDE = r"""
var __access = [];
var SH_api = {
  getAccess: function (plural, id) {
    __access.push(["get", plural, id]);
    return Promise.resolve({ owners: [], grants: [], public: false, public_tools: [], rev: 3 });
  },
  setAccess: function (plural, id, body) {
    __access.push(["put", plural, id, body]);
    return Promise.resolve(body);
  },
};
"""


def test_make_public_reads_the_stored_view_then_puts_public_on_it() -> None:
    """The POST cannot set the access (the server stamps a private
    ``Access`` and ignores the body), so the switch's PUT carries what the
    GET stored: the rev, the (empty) owners, no grants, public on, no
    public tools (a use grant has none)."""
    from tests.ui._mini_react import mini_react_context

    ctx = mini_react_context(_transpiled(SHARE_SRC), prelude=_MAKE_PUBLIC_PRELUDE)
    try:
        ctx.eval("SHR_makePublic('model_profiles', 'p1')")
        calls = json.loads(ctx.eval("JSON.stringify(__access)"))
    finally:
        ctx.close()
    assert calls == [
        ["get", "model_profiles", "p1"],
        ["put", "model_profiles", "p1", {"owners": [], "grants": {}, "public": True, "public_tools": [], "rev": 3}],
    ]


# ---- the dialog's useOnly mode (the real file in MiniRacer) ------------------

# The dialog's globals, stubbed the way tests/ui/test_share_dialog.py does
# (Modal/Btn pass their props through; the access read is a state the test
# sets; the write side is a queue), plus the console's one reader of the
# caller's role: the useOnly dialog edits for an admin and reads-only for
# everyone else (the view's your_level is not the editor's gate: these rows
# have no owners, and a grantee's served view hides the grants).
DIALOG_PRELUDE = r"""
var __calls = [];
var __resState = { data: undefined, error: null, loading: false };
var __refetches = 0;
var __puts = [];
var __putResults = [];
var __lookups = [];
var __role = "admin";
var NV_useConsole = function () { return { role: __role }; };
function thenOk(v) { return { then: function (f, r) { if (f) f(v); return thenOk(v); }, catch: function () { return this; } }; }
function thenErr(e) { return { then: function (f, r) { if (r) r(e); return thenErr(e); }, catch: function (f) { if (f) f(e); return thenOk(undefined); } }; }
window.primerApi = {
  useResource: function (key, fetcher, opts) {
    return { data: __resState.data, error: __resState.error, loading: __resState.loading, degraded: false,
      refetch: function () { __refetches++; } };
  },
  useMutation: function (fetcher, opts) {
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
  toastPush: function (t) { __calls.push(["toast", t.kind, t.title]); },
  useViewport: function () { return { isMobile: false }; },
};
var SH_api = {
  getAccess: function (plural, id, signal) { return thenOk(__resState.data); },
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

# What the server serves an ADMIN for a use grant: no owners, read grants,
# the public switch and the rev (get_access_view, use_only).
USE_VIEW = {
    "owners": [],
    "grants": [{"user": {"id": "u-2", "username": "bob"}, "level": "read"}],
    "public": False,
    "public_tools": [],
    "rev": 3,
    "your_level": "owner",
}
# What a non-admin grantee is served: only the level and the public flag.
USE_VIEW_GRANTEE = {
    "owners": [],
    "grants": [],
    "public": True,
    "public_tools": [],
    "rev": 0,
    "your_level": "read",
}
MOUNT_USE = (
    'MR.mount(SHR_ShareDialog, { plural: "model_profiles", id: "p1", title: "Profile p1", '
    "useOnly: true, onClose: function () {} })"
)


def _use_ctx(view: dict | None = None, role: str = "admin"):
    from tests.ui._mini_react import mini_react_context

    prelude = DIALOG_PRELUDE
    if role != "admin":
        prelude = prelude.replace('var __role = "admin";', 'var __role = ' + json.dumps(role) + ';')
    if view is not None:
        prelude += "__resState = { data: " + json.dumps(view) + ", error: null, loading: false };"
    return mini_react_context(_transpiled(SHARE_SRC), prelude=prelude)


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_an_admin_edits_the_use_grants_without_owners_or_levels() -> None:
    """useOnly: no owners (these rows have none) and no level picker
    (read is the only level); the section is "People who can use" and the
    switch "Everyone can use"; the save PUTs owners [] with read grants
    only."""
    ctx = _use_ctx(USE_VIEW)
    try:
        ctx.eval(MOUNT_USE)
        texts = _texts(ctx)
        assert "People who can use" in texts
        assert "Everyone can use" in texts
        assert "Owners" not in texts, "a use grant has no owners to show"
        assert json.loads(ctx.eval('JSON.stringify(MR.findAll("shr-grant-level-").length)')) == 0, (
            "a use grant has no level to pick"
        )
        assert ctx.eval('MR.find("shr-save") ? 1 : 0') == 1, "an admin edits use grants"
        assert ctx.eval('MR.find("shr-readonly") ? 1 : 0') == 0
        # Make it public and add one more person, then save.
        ctx.eval('MR.findProp("data-testid", "shr-public").props.onChange({ target: { checked: true } })')
        ctx.eval("MR.rerender()")
        ctx.eval('MR.findProp("data-testid", "shr-add-user").props.onChange({ target: { value: "bo" } })')
        ctx.eval("MR.rerender()")
        ctx.eval('MR.click("shr-lookup-u-3")')
        ctx.eval('MR.click("shr-save")')
        puts = json.loads(ctx.eval("JSON.stringify(__puts)"))
    finally:
        ctx.close()
    assert puts and puts[-1][0] == "model_profiles" and puts[-1][1] == "p1"
    assert puts[-1][2] == {
        "owners": [],
        "grants": {"u-2": "read", "u-3": "read"},
        "public": True,
        "public_tools": [],
        "rev": 3,
    }


def test_a_grantee_reads_the_use_grants_only() -> None:
    """The dialog edits for an admin and reads-only for everyone else:
    no Save, no add control, the switch held off, one line saying an admin
    is the one who changes it."""
    ctx = _use_ctx(USE_VIEW_GRANTEE, role="user")
    try:
        ctx.eval(MOUNT_USE)
        assert ctx.eval('MR.find("shr-readonly") ? 1 : 0') == 1
        assert "Only an admin can change who can use this." in _texts(ctx)
        assert ctx.eval('MR.find("shr-save") ? 1 : 0') == 0
        assert ctx.eval('MR.find("shr-add-user") ? 1 : 0') == 0
        assert ctx.eval('MR.find("shr-public").props.disabled === true')
    finally:
        ctx.close()


# ---- the agent form and the graph node say a profile is not available -------

from tests.ui._agent_form_harness import (  # noqa: E402
    PROFILE_CHOICES_KEY,
    PROFILE_ROWS,
    agent_form,
    mount,
    options,
    prop,
    set_resource,
)


def test_the_agent_form_says_a_stored_profile_outside_the_choices_is_not_available() -> None:
    """The form reads /model_profiles/choices (user tier: what the caller
    can USE), so a profile the agent names that is not in the list is not
    deleted - it is not available to the caller. The selection keeps the
    id (a save must not repoint the agent) and says so, with the warning
    the plan fixes."""
    with agent_form() as ctx:
        set_resource(ctx, PROFILE_CHOICES_KEY, {"data": {"items": [PROFILE_ROWS[0]]}, "loading": False, "error": None})
        mount(ctx, existing={"id": "plain", "description": "Plain", "model": {"profile_id": "slow"}, "tools": []})
        opts = options(ctx, "agent-model-profile")
        assert {"value": "slow", "text": "slow (not available to you)"} in opts, (
            "the stored profile stays selectable and names why"
        )
        said = prop(ctx, "field:Model profile", "data-error")
        assert said == "This model profile is not available to you. An admin can grant its use."


def test_the_agent_form_keeps_saying_available_for_a_profile_in_the_choices() -> None:
    """The warning is about availability, not existence: a profile the
    caller can use is plain, with no complaint."""
    with agent_form() as ctx:
        mount(ctx, existing={"id": "plain", "description": "Plain", "model": {"profile_id": "fast"}, "tools": []})
        opts = options(ctx, "agent-model-profile")
        assert {"value": "fast", "text": "fast"} in opts
        assert "(not available to you)" not in " ".join(o["text"] for o in opts)
        assert not prop(ctx, "field:Model profile", "data-error")


def test_the_graph_agent_node_picker_says_a_profile_not_in_the_choices_is_not_available() -> None:
    """A graph agent node may name a model profile of its own (overriding
    the agent's). The inspector's picker reads the same choices; a named
    profile that is not in them keeps the id, says "(not available to you)"
    and warns. The stand-in answers the choices read with its tool rows
    (ids the node does not name), so the node's profile is outside them."""
    from tests.ui._graph_builder_v8 import DRIVER, PRELUDE, builder_code
    from tests.ui._mini_react import mini_react_context

    node = {"kind": "agent", "id": "n1", "description": "Triage", "agent_id": "a1", "profile_id": "secret", "x": 0, "y": 0}
    draft = {"description": "g", "nodes": [node], "edges": []}
    ctx = mini_react_context(builder_code(), PRELUDE)
    try:
        ctx.eval(DRIVER)
        ctx.eval("MR.mount(GB_Inspector, inspectorProps(" + json.dumps(draft) + ", " + json.dumps(node) + ", null))")
        assert ctx.eval('MR.find("gb-profile-picker") ? 1 : 0') == 1
        texts = " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))
        assert "secret (not available to you)" in texts
        assert ctx.eval('MR.find("gb-profile-unusable") ? 1 : 0') == 1
    finally:
        ctx.close()


# ---- the paused session's line ------------------------------------------------


def _banner_code() -> str:
    """The one function the doc draws the line with: a gate (status
    paused, the code model_profile_unusable) and the plan's exact words."""
    start = SESSION_DOC.find("function NV_pausedUnusableBanner(")
    assert start >= 0, "NV_pausedUnusableBanner is not in nv-session-doc.jsx"
    return SESSION_DOC[start : SESSION_DOC.index("\n}\n", start) + len("\n}\n")]


def test_the_paused_line_is_the_plans_exact_words_and_only_for_its_gate() -> None:
    from tests.ui._mini_react import mini_react_context

    ctx = mini_react_context(_banner_code())
    try:
        paused = {"status": "paused", "last_turn_error": {"code": "model_profile_unusable", "at": "2026-10-31T09:00:00Z"}}
        assert ctx.eval("NV_pausedUnusableBanner(" + json.dumps(paused) + ")") == EXACT_TEXT
        for other in (
            {"status": "waiting", "last_turn_error": {"code": "model_profile_unusable"}},
            {"status": "paused", "last_turn_error": {"code": "provider_timeout"}},
            {"status": "paused", "last_turn_error": None},
            {"status": "paused"},
            None,
        ):
            assert ctx.eval("JSON.stringify(NV_pausedUnusableBanner(" + json.dumps(other) + "))") == "null"
    finally:
        ctx.close()


def test_the_session_doc_draws_the_paused_line() -> None:
    """The line is drawn by the document (below the session header, as the
    other session-level banners) whenever the gate holds; the ERROR record
    of the failed turn renders its own message, so this says the viewer's
    part only."""
    assert "NV_pausedUnusableBanner(session)" in SESSION_DOC
    assert 'data-testid="nv-profile-unusable-banner"' in SESSION_DOC


# ---- the overlays, the cards, the create forms (the real lines) --------------


def test_the_detail_overlays_offer_who_can_use() -> None:
    """The "Who can use" button and the useOnly dialog sit on the six
    detail overlays: the two model profile overlays, the three panel
    provider details and the register overlay's edit mode for the wizard
    classes (LLM / embedding / cross-encoder open their instance in the
    register overlay, so the wizard pages carry the affordance)."""
    for name, src in (
        ("model-profiles.jsx", MODEL_PROFILES),
        ("workspaces/providers.jsx", WP_PROVIDERS),
        ("channels.jsx", CHANNELS),
        ("semantic-search.jsx", SEMANTIC),
        ("providers/register-llm.jsx", REG_LLM),
        ("providers/register-models.jsx", REG_MODELS),
    ):
        assert "SHR_UseShare" in src, name + " does not offer 'Who can use'"
    # The plurals the access router names (infra_access._KINDS).
    assert 'plural="model_profiles"' in MODEL_PROFILES
    assert 'plural="workspace_providers"' in WP_PROVIDERS
    assert 'plural="channel_providers"' in CHANNELS
    assert 'plural="ssp"' in SEMANTIC
    # The wizard pages name their class's plural from klass.
    assert "SHR_UseShare" in REG_LLM and "klass.plural" in REG_LLM
    assert "SHR_UseShare" in REG_MODELS and "klass.plural" in REG_MODELS


def test_the_lists_and_cards_badge_the_use_access() -> None:
    """A list says at a glance which rows are public, how many people can
    use them, or admin-only, without opening them: the badge is drawn from
    the row's access field (the grantable CRUD shape)."""
    for name, src in (
        ("model-profiles.jsx", MODEL_PROFILES),
        ("workspaces/providers.jsx", WP_PROVIDERS),
        ("channels.jsx", CHANNELS),
        ("semantic-search.jsx", SEMANTIC),
        ("provider-catalog.jsx", CATALOG),
    ):
        assert "SHR_useBadge" in src, name + " does not badge the row's access"
    # The catalog badges the grantable classes only (the global-settings
    # classes have no grant: web search, web fetch, speech, artifact store).
    assert "cross_encoder" in CATALOG and "SHR_useBadge(row.access)" in CATALOG


def test_the_create_forms_offer_the_public_switch() -> None:
    """New rows default private (lead ruling 2), so each create form of a
    grantable kind offers the "Public" switch (off by default) and makes
    the new row public through the follow-up access PUT when it is on."""
    for name, src, switch, plural in (
        ("model-profiles.jsx", MODEL_PROFILES, "mp-aggregate-public", "model_profiles"),
        ("workspaces/providers.jsx", WP_PROVIDERS, "wp-create-public", "workspace_providers"),
        ("channels.jsx", CHANNELS, "ch-provider-public", "channel_providers"),
        ("semantic-search.jsx", SEMANTIC, "ssp-create-public", "ssp"),
        ("providers/register-llm.jsx", REG_LLM, "pc-llm-public", "llm_providers"),
        ("providers/register-models.jsx", REG_MODELS, "pc-models-public", "embedding_providers"),
    ):
        assert f'data-testid="{switch}"' in src, name + " has no Public switch on its create form"
        assert "SHR_makePublic" in src, name + " does not make the new row public through the access PUT"
        assert plural in src


def test_the_agent_form_no_longer_says_the_profile_is_missing() -> None:
    """The plan's words replace "(missing)": the profile is not deleted,
    it is not available to the caller."""
    assert 'profileId + " (not available to you)"' in AGENTS
    assert "(missing)" not in AGENTS
    assert "This model profile is not available to you. An admin can grant its use." in AGENTS


def test_the_graph_inspector_reads_the_choices() -> None:
    """The picker offers the same user-tier choices the agent form reads,
    and says a named profile outside them is not available."""
    assert "/model_profiles/choices" in GB_INSPECTOR
    assert "(not available to you)" in GB_INSPECTOR
    assert 'data-testid="gb-profile-unusable"' in GB_INSPECTOR
