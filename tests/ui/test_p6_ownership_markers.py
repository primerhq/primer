"""ACL-P6-T3: every owned entity's list and detail says whose it is.

The marker reader ``SHR_accessMarkers(access, me, names)`` (share-dialog.jsx)
answers, from the row's stored access (``owners`` a list of ids, ``grants``
the uid -> level MAP, ``public`` a flag) and the viewer's user id:
"Owned by you" when the viewer owns it, "Owned by <name>" (names from
``SHR_useUserNames``; an id that names no user says the id) when they do not,
"Shared with you (read|full)" for a grant the viewer holds, and "Public" for
a public row the viewer does not own (Decision 8). It replaces the old
``SHR_accessBadges`` (which never drew the owner and read the grant count
only). This file pins the helper's cases (the real source in MiniRacer),
pins the object-``grants`` shape the old reader missed, pins that every owned
kind's list AND detail draws the markers, that the seven grantable pages keep
the "Who can use" button, that the trigger detail says who it runs as (and
the deleted-user copy when the name names no user), the lifecycle copy of the
user-management dialogs, and the phone-width classes of the mobile pass.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

SHARE_SRC = UI / "components" / "shared" / "share-dialog.jsx"
AGENTS = (UI / "components" / "agents.jsx").read_text(encoding="utf-8")
GRAPHS = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")
TOOLSETS = (UI / "components" / "toolsets.jsx").read_text(encoding="utf-8")
CHANNELS = (UI / "components" / "channels.jsx").read_text(encoding="utf-8")
TEMPLATES = (UI / "components" / "workspaces" / "templates.jsx").read_text(encoding="utf-8")
WORKSPACES = (UI / "components" / "workspaces.jsx").read_text(encoding="utf-8")
NV_RAIL = (UI / "components" / "console" / "nv-rail.jsx").read_text(encoding="utf-8")
MOBILE = (UI / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
KNOWLEDGE = (UI / "components" / "knowledge.jsx").read_text(encoding="utf-8")
TRIGGERS = (UI / "components" / "triggers.jsx").read_text(encoding="utf-8")
HARNESSES = (UI / "components" / "harnesses.jsx").read_text(encoding="utf-8")
ADMIN_USERS = (UI / "components" / "admin_users.jsx").read_text(encoding="utf-8")
MODEL_PROFILES = (UI / "components" / "model-profiles.jsx").read_text(encoding="utf-8")
REG_LLM = (UI / "components" / "providers" / "register-llm.jsx").read_text(encoding="utf-8")
REG_MODELS = (UI / "components" / "providers" / "register-models.jsx").read_text(encoding="utf-8")
WP_PROVIDERS = (UI / "components" / "workspaces" / "providers.jsx").read_text(encoding="utf-8")
SEMANTIC = (UI / "components" / "semantic-search.jsx").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")


def _transpiled(path: Path) -> str:
    from tests.ui._mini_react import transpile

    return transpile(path)


# ---- the marker reader (the real file in MiniRacer) -------------------------

# No hook in the picture: the reader is a pure function of (access, me, names).
PRELUDE = r"""
"""


def _ctx():
    from tests.ui._mini_react import mini_react_context

    return mini_react_context(_transpiled(SHARE_SRC), prelude=PRELUDE)


def test_the_marker_cases() -> None:
    """Every Decision 8 case, and a row with no access block says nothing."""
    ctx = _ctx()
    try:
        def markers(access: str, me: str, names: str) -> list:
            return json.loads(
                ctx.eval(
                    "JSON.stringify(SHR_accessMarkers(" + access + ", " + me + ", " + names + "))"
                )
            )

        assert markers("null", "null", "{}") == []
        assert markers("undefined", "null", "{}") == []
        assert markers("{owners: [], grants: {}, public: false}", "'u1'", "{}") == []
        # The viewer owns it: one marker, no name list of their own.
        assert markers("{owners: ['u1'], grants: {}, public: false}", "'u1'", "{}") == ["Owned by you"]
        # The viewer does not own it: the owner's name, and only their name.
        assert markers("{owners: ['u2'], grants: {}, public: false}", "'u1'", '{u2: "bob"}') == ["Owned by bob"]
        # An id that names no user (deleted) says the id, not a hole.
        assert markers("{owners: ['u2'], grants: {}, public: false}", "'u1'", "{}") == ["Owned by u2"]
        # Co-ownership names every owner.
        assert markers("{owners: ['u2', 'u3'], grants: {}, public: false}", "'u1'", '{u2: "bob", u3: "carl"}') \
            == ["Owned by bob, carl"]
        # A grant the viewer holds: the owner marker AND the grant marker.
        assert markers("{owners: ['u2'], grants: {'u1': 'read'}, public: false}", "'u1'", '{u2: "bob"}') \
            == ["Owned by bob", "Shared with you (read)"]
        assert markers("{owners: ['u2'], grants: {'u1': 'full'}, public: false}", "'u1'", '{u2: "bob"}') \
            == ["Owned by bob", "Shared with you (full)"]
        # Ownership outranks the viewer's own grant (a row does not say both).
        assert markers("{owners: ['u1'], grants: {'u1': 'read'}, public: false}", "'u1'", "{}") == ["Owned by you"]
        # Public: only said for a row the viewer does not own.
        assert markers("{owners: ['u2'], grants: {}, public: true}", "'u1'", '{u2: "bob"}') \
            == ["Owned by bob", "Public"]
        assert markers("{owners: ['u1'], grants: {}, public: true}", "'u1'", "{}") == ["Owned by you"]
        # No viewer id (signed out): the owners are still named, nothing personal.
        assert markers("{owners: ['u2'], grants: {'u2': 'read'}, public: false}", "null", '{u2: "bob"}') \
            == ["Owned by bob"]
    finally:
        ctx.close()


def test_the_marker_reads_grants_as_an_object() -> None:
    """The row's access is the Access model: ``grants`` is the uid -> level
    MAP (the /access view's array shape only arrives through the dialog). A
    grant the viewer holds must be said from the map's key - the old
    array-only reader never saw it."""
    ctx = _ctx()
    try:
        def markers(access: str, me: str) -> list:
            return json.loads(ctx.eval("JSON.stringify(SHR_accessMarkers(" + access + ", " + me + ", {}))"))

        assert markers("{owners: [], grants: {'u1': 'read'}, public: false}", "'u1'") == ["Shared with you (read)"]
        assert markers("{owners: [], grants: {'u1': 'full'}, public: false}", "'u1'") == ["Shared with you (full)"]
        # A grant for SOMEONE ELSE is not the viewer's marker.
        assert markers("{owners: [], grants: {'u2': 'read'}, public: false}", "'u1'") == []
    finally:
        ctx.close()


def test_the_old_badge_reader_is_replaced() -> None:
    """``SHR_accessBadges`` is retired: the marker reader is the one reader
    of a row's access for the ownership surface."""
    src = SHARE_SRC.read_text(encoding="utf-8")
    assert "SHR_accessBadges" not in src, "the old badge reader is still in share-dialog.jsx"
    assert "window.SHR_accessMarkers = SHR_accessMarkers" in src
    assert "window.SHR_AccessMarkers = SHR_AccessMarkers" in src


# ---- the lists and detail headers of every owned kind ------------------------


def test_every_owned_list_draws_the_markers_from_the_row() -> None:
    """Every owned kind's list draws the markers from the row's access (with
    the viewer id and the looked-up names), so a list says whose each row is
    without opening it. The workspace switcher (the rail) says it with the
    ownership groups instead: Mine / Shared with me / Public."""
    for name, src, needle in (
        ("agents.jsx", AGENTS, "SHR_AccessMarkers access={a.access}"),
        ("graphs.jsx", GRAPHS, "SHR_AccessMarkers access={g.access}"),
        ("toolsets.jsx", TOOLSETS, "SHR_AccessMarkers access={t.access}"),
        ("channels.jsx", CHANNELS, "SHR_AccessMarkers access={c.access}"),
        ("workspaces/templates.jsx", TEMPLATES, "SHR_accessMarkers(t.access"),
        ("knowledge.jsx", KNOWLEDGE, "SHR_AccessMarkers access={c.access}"),
        ("triggers.jsx", TRIGGERS, "SHR_AccessMarkers access={trigger.access}"),
        ("harnesses.jsx", HARNESSES, "SHR_AccessMarkers access={h.access}"),
    ):
        assert needle in src, name + " does not draw its ownership markers from the row"
        # The names go through the shared hook (one batched lookup per set).
        assert "SHR_useUserNames(" in src, name + " does not look up the owner names"
    assert "NV_wsGroupOf" in NV_RAIL, "the rail switcher does not group by ownership"


def test_every_owned_detail_header_draws_the_markers() -> None:
    """The detail surface of every owned kind draws the markers too (the row's
    own access, the viewer id, looked-up names) - not only the list."""
    for name, src, needle in (
        ("agents.jsx", AGENTS, 'data-testid="agent-access-markers"'),
        ("graphs.jsx", GRAPHS, 'data-testid="graph-access-markers"'),
        ("toolsets.jsx", TOOLSETS, 'data-testid="toolset-access-markers"'),
        ("channels.jsx", CHANNELS, 'data-testid="channel-access-markers"'),
        ("workspaces/templates.jsx", TEMPLATES, 'data-testid="template-access-markers"'),
        ("workspaces.jsx", WORKSPACES, 'data-testid="workspace-access-markers"'),
        ("knowledge.jsx", KNOWLEDGE, 'data-testid="collection-access-markers"'),
        ("triggers.jsx", TRIGGERS, 'data-testid="trigger-access-markers"'),
        ("harnesses.jsx", HARNESSES, 'data-testid="harness-access-markers"'),
    ):
        assert needle in src, name + " detail does not draw the ownership markers"


def test_the_marker_row_is_the_shared_component() -> None:
    """The pill row every surface draws is the shared component (one class
    for the phone's wrap, one testid scheme), not per-list markup."""
    ctx = _ctx()
    try:
        ctx.eval(
            "MR.mount(function (p) { return React.createElement(SHR_AccessMarkers, { access: {owners: ['u2'], grants: {}, public: true}, me: 'u1', names: {u2: 'bob'} }); })"
        )
        out = json.loads(ctx.eval("JSON.stringify(MR.texts())"))
        assert "Owned by bob" in " ".join(out) and "Public" in " ".join(out)
    finally:
        ctx.close()


# ---- the trigger detail says who its fires run as ---------------------------


def test_the_trigger_detail_says_who_it_runs_as() -> None:
    """The trigger's fires run as its ``owner`` (not its access): the detail
    says the username, or the take-over copy when the id names no user
    (deleted), or "an ordinary user" on a row saved before owners were
    recorded."""
    assert 'data-testid="trigger-runs-as"' in TRIGGERS
    assert "Runs as a deleted user: edit it to take it over" in TRIGGERS
    assert "Runs as an ordinary user" in TRIGGERS
    # The name goes through the shared hook (one batched lookup with the
    # access owners).
    assert "SHR_useUserNames(" in TRIGGERS


# ---- the seven grantable pages keep the "Who can use" button ----------------


def test_the_seven_grantable_pages_offer_who_can_use() -> None:
    """Phase 4 Task 7's button on each of the seven use-grant kinds (the
    plurals the access router names): the wizard pages name their class's
    plural from ``klass``, so both provider kinds on a wizard ride the same
    button."""
    assert 'plural="model_profiles"' in MODEL_PROFILES and "SHR_UseShare" in MODEL_PROFILES
    assert "SHR_UseShare" in REG_LLM and "klass.plural" in REG_LLM
    assert "SHR_UseShare" in REG_MODELS and "klass.plural" in REG_MODELS
    assert "SHR_UseShare" in WP_PROVIDERS and 'plural="workspace_providers"' in WP_PROVIDERS
    assert "SHR_UseShare" in CHANNELS and 'plural="channel_providers"' in CHANNELS
    assert "SHR_UseShare" in SEMANTIC and 'plural="ssp"' in SEMANTIC


# ---- the lifecycle copy of the user-management dialogs ----------------------


def test_the_delete_dialog_says_what_the_deletion_does() -> None:
    """The admin's delete dialog names the hand-over: their entities move to
    you, their sharing grants and MCP sign-ins are removed, and triggers that
    run as them stop until someone with full access edits them (the lead's
    answer: disabled on deletion)."""
    assert "move to you" in ADMIN_USERS
    assert "MCP sign-ins are removed" in ADMIN_USERS
    assert "switched off until" in ADMIN_USERS
    assert "full access edits them" in ADMIN_USERS


def test_the_edit_dialog_says_what_demotion_keeps() -> None:
    """A restricted or disabled user keeps their entities and cannot use them
    until restored - said in the edit dialog (the demotion surface)."""
    assert "keeps their entities and cannot use them until restored" in ADMIN_USERS


# ---- the phone-width pass ----------------------------------------------------


def test_the_phone_markers_wrap_without_overflow() -> None:
    """The marker row wraps on the phone (a card has no room for a run-on
    "Owned by long username"); the row's class is the CSS hook."""
    assert ".shr-markers" in CSS
    assert "flex-wrap" in CSS[CSS.index(".shr-markers"): CSS.index(".shr-markers") + 400]
    # The dialog stays the house bottom sheet on a phone (Modal draws the
    # sheet tree; both the Share and the "Who can use" dialogs ride it).
    share = SHARE_SRC.read_text(encoding="utf-8")
    assert "<Modal" in share
    assert ".sheet-overlay" in CSS


def test_the_phone_workspace_switcher_keeps_the_groups() -> None:
    """The phone's Spaces tab groups the workspaces the same way the desktop
    rail does (Mine / Shared with me / Public, NV_wsGroupOf), so the switcher
    says whose each workspace is on a phone too."""
    assert "NV_wsGroupOf" in MOBILE
    assert "Mine" in MOBILE and "Shared with me" in MOBILE


def test_the_phone_delete_dialog_is_the_sheet() -> None:
    """The delete dialog rides the house Modal (bottom sheet on a phone), so
    its longer copy wraps in the sheet body rather than overflowing."""
    i = ADMIN_USERS.index("function ADM_DeleteUserDialog")
    block = ADMIN_USERS[i : i + 4000]
    assert "<Modal" in block
