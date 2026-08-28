"""Tab-group model (US-007 R2, phase 1): the pure multi-group state machine.

Loaded in a MiniRacer context with `var window = globalThis;`, no DOM
(the foundation modules' shared unit-test pattern). We drive the
pure model (TG_init / TG_openTab / ...) through window.* and inspect the
resulting model, so the state machine is unit-tested without a real tab
bar or a real drag-and-drop gesture.

Covers uiv2/implementer-notes.md section 2.3: preview-tab replacement,
promote-on-move, last-tab-close collapsing the layout (and leaving one
empty group when everything closes), the one-direction-at-a-time split
lock, and focus semantics (opening/moving always focuses the destination
group; explicit focus changes never touch tabs).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "tab-group-model.js"


def _ctx() -> "py_mini_racer.MiniRacer":
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _call(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _init(ctx, group_id="g1"):
    return _call(ctx, f'TG_init({{groupId: "{group_id}"}})')


def _doc(kind, ref):
    return {"kind": kind, "ref": ref}


def _model_js(model) -> str:
    return json.dumps(model)


# ---------------------------------------------------------------------------
# Tab identity
# ---------------------------------------------------------------------------

def test_tab_id_is_kind_colon_ref() -> None:
    ctx = _ctx()
    assert _call(ctx, 'TG_tabId("session", "abc123")') == "session:abc123"


def test_init_starts_with_one_empty_group_focused() -> None:
    ctx = _ctx()
    m = _init(ctx)
    assert len(m["groups"]) == 1
    assert m["groups"][0]["tabs"] == []
    assert m["direction"] == "row"
    assert m["focusedGroupId"] == m["groups"][0]["id"] == "g1"


# ---------------------------------------------------------------------------
# openTab: preview replacement, promote, reopen-focuses, default group
# ---------------------------------------------------------------------------

def test_open_preview_tab_replaces_existing_preview_at_same_slot() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "b.txt"))))
    tabs = m["groups"][0]["tabs"]
    assert len(tabs) == 1, "a second preview must replace the first, not accrete"
    assert tabs[0]["ref"] == "b.txt"
    assert tabs[0]["preview"] is True


def test_open_promoted_tab_does_not_replace_the_preview() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "b.txt"))))
    tabs = m["groups"][0]["tabs"]
    assert [t["ref"] for t in tabs] == ["a.txt", "b.txt"]
    assert tabs[0]["preview"] is True
    assert tabs[1]["preview"] is False


def test_reopening_an_open_tab_does_not_duplicate_and_focuses_its_group() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    before = m
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    assert len(m["groups"][0]["tabs"]) == 1
    assert m["focusedGroupId"] == before["groups"][0]["id"]
    assert m["groups"][0]["activeTabId"] == "session:s1"


def test_reopening_a_preview_tab_with_promote_promotes_it_in_place() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    assert m["groups"][0]["tabs"][0]["preview"] is True
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    assert len(m["groups"][0]["tabs"]) == 1
    assert m["groups"][0]["tabs"][0]["preview"] is False


def test_open_tab_defaults_to_the_focused_group() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    m2 = _call(
        ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("session:s1"), json.dumps("row"))
    )
    # Now the sole tab lives in the NEW group; opening another doc with no
    # explicit groupId must land in whichever group is focused (the new one).
    m3 = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m2), _model_js(_doc("file", "x.txt"))))
    focused = [g for g in m3["groups"] if g["id"] == m3["focusedGroupId"]][0]
    assert any(t["ref"] == "x.txt" for t in focused["tabs"])


# ---------------------------------------------------------------------------
# promoteTab
# ---------------------------------------------------------------------------

def test_promote_tab_flips_the_preview_flag() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_promoteTab({}, {})".format(_model_js(m), json.dumps("file:a.txt")))
    assert m["groups"][0]["tabs"][0]["preview"] is False


def test_promote_tab_is_a_noop_for_an_unknown_id() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    before = m
    m = _call(ctx, "TG_promoteTab({}, {})".format(_model_js(m), json.dumps("file:missing.txt")))
    assert m == before


# ---------------------------------------------------------------------------
# closeTab: last-tab-close collapse
# ---------------------------------------------------------------------------

def test_closing_a_groups_last_tab_removes_the_group() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    # a.txt stays behind in the original group; s1 gets its own group.
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("session:s1"), json.dumps("row")))
    assert len(m["groups"]) == 2
    m = _call(ctx, "TG_closeTab({}, {})".format(_model_js(m), json.dumps("session:s1")))
    assert len(m["groups"]) == 1, "closing the only tab in a group must remove that group"
    assert m["direction"] == "row", "a single remaining group resets the split direction"
    assert [t["ref"] for t in m["groups"][0]["tabs"]] == ["a.txt"]


def test_closing_the_last_tab_everywhere_leaves_one_empty_group() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_closeTab({}, {})".format(_model_js(m), json.dumps("file:a.txt")))
    assert len(m["groups"]) == 1
    assert m["groups"][0]["tabs"] == []
    assert m["groups"][0]["activeTabId"] is None
    assert m["focusedGroupId"] == m["groups"][0]["id"]


def test_closing_a_non_active_tab_leaves_the_active_tab_unchanged() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "b.txt"))))
    # Re-select a.txt as active without touching b.txt's presence.
    m = _call(ctx, "TG_openTab({}, {}, {{}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    assert m["groups"][0]["activeTabId"] == "file:a.txt"
    m = _call(ctx, "TG_closeTab({}, {})".format(_model_js(m), json.dumps("file:b.txt")))
    assert m["groups"][0]["activeTabId"] == "file:a.txt"
    assert [t["ref"] for t in m["groups"][0]["tabs"]] == ["a.txt"]


def test_closing_the_active_tab_falls_back_to_the_last_remaining_tab() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "b.txt"))))
    assert m["groups"][0]["activeTabId"] == "file:b.txt"
    m = _call(ctx, "TG_closeTab({}, {})".format(_model_js(m), json.dumps("file:b.txt")))
    assert m["groups"][0]["activeTabId"] == "file:a.txt"


# ---------------------------------------------------------------------------
# moveTab: promote-on-move, cross-group collapse, same-group reorder
# ---------------------------------------------------------------------------

def test_move_tab_always_promotes_even_a_preview_tab() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    # a.txt stays behind so group1 survives; s1 gets group2 to itself.
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("session:s1"), json.dumps("row")))
    group1_id = [g["id"] for g in m["groups"] if any(t["ref"] == "a.txt" for t in g["tabs"])][0]
    group2_id = [g["id"] for g in m["groups"] if g["id"] != group1_id][0]
    m = _call(ctx, "TG_openTab({}, {}, {{groupId: {}}})".format(_model_js(m), _model_js(_doc("file", "b.txt")), json.dumps(group1_id)))
    assert [t for t in m["groups"][0]["tabs"] if t["ref"] == "b.txt"][0]["preview"] is True
    m = _call(ctx, "TG_moveTab({}, {}, {}, null)".format(_model_js(m), json.dumps("file:b.txt"), json.dumps(group2_id)))
    moved_group = [g for g in m["groups"] if g["id"] == group2_id][0]
    moved = [t for t in moved_group["tabs"] if t["ref"] == "b.txt"][0]
    assert moved["preview"] is False, "moveTab must promote a preview tab on arrival"
    assert m["focusedGroupId"] == group2_id


def test_move_tab_collapses_an_emptied_source_group() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("session:s1"), json.dumps("column")))
    assert len(m["groups"]) == 2
    group_with_a = [g for g in m["groups"] if any(t["ref"] == "a.txt" for t in g["tabs"])][0]
    group_with_s1 = [g for g in m["groups"] if g["id"] != group_with_a["id"]][0]
    # s1 is the ONLY tab in its group; moving it away must collapse that group.
    m = _call(ctx, "TG_moveTab({}, {}, {}, null)".format(_model_js(m), json.dumps("session:s1"), json.dumps(group_with_a["id"])))
    assert len(m["groups"]) == 1, "moving the only tab out of a group must collapse it"
    assert m["direction"] == "row"


def test_move_tab_within_the_same_group_reorders() -> None:
    ctx = _ctx()
    m = _init(ctx)
    for ref in ("a.txt", "b.txt", "c.txt"):
        m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", ref))))
    gid = m["groups"][0]["id"]
    m = _call(ctx, "TG_moveTab({}, {}, {}, 0)".format(_model_js(m), json.dumps("file:c.txt"), json.dumps(gid)))
    assert [t["ref"] for t in m["groups"][0]["tabs"]] == ["c.txt", "a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# splitWith: insertion position + the one-direction-at-a-time lock
# ---------------------------------------------------------------------------

def test_split_inserts_the_new_group_immediately_after_the_source() -> None:
    ctx = _ctx()
    m = _init(ctx)
    for ref in ("a.txt", "b.txt"):
        m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", ref))))
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("file:b.txt"), json.dumps("row")))
    assert len(m["groups"]) == 2
    assert [t["ref"] for t in m["groups"][0]["tabs"]] == ["a.txt"]
    assert [t["ref"] for t in m["groups"][1]["tabs"]] == ["b.txt"]
    assert m["focusedGroupId"] == m["groups"][1]["id"]


def test_split_onto_a_different_group_inserts_after_that_group() -> None:
    """A tab dragged from its own group can be split against a DIFFERENT
    group's drop zone - the new sibling lands after the drop target, not
    after the dragged tab's own source group."""
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    group1_id = m["groups"][0]["id"]
    # Split s1 out of group1 (default target = its own source): group1=[a],
    # group2=[s1], inserted right after group1.
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("session:s1"), json.dumps("row")))
    group2_id = [g["id"] for g in m["groups"] if g["id"] != group1_id][0]
    m = _call(ctx, "TG_openTab({}, {}, {{groupId: {}, promote: true}})".format(_model_js(m), _model_js(_doc("file", "c.txt")), json.dumps(group1_id)))
    assert [t["ref"] for t in [g for g in m["groups"] if g["id"] == group1_id][0]["tabs"]] == ["a.txt", "c.txt"]
    # Drag c.txt (living in group1) and drop it on group2's split zone: the
    # new group must land after group2, NOT after group1 (c.txt's source).
    m = _call(
        ctx,
        "TG_splitWith({}, {}, {}, {})".format(_model_js(m), json.dumps("file:c.txt"), json.dumps("row"), json.dumps(group2_id)),
    )
    assert len(m["groups"]) == 3
    ids_in_order = [g["id"] for g in m["groups"]]
    assert ids_in_order.index(group2_id) + 1 == ids_in_order.index(m["focusedGroupId"])
    assert [t["ref"] for t in [g for g in m["groups"] if g["id"] == group1_id][0]["tabs"]] == ["a.txt"]
    new_group = [g for g in m["groups"] if g["id"] not in (group1_id, group2_id)][0]
    assert [t["ref"] for t in new_group["tabs"]] == ["c.txt"]
    assert m["focusedGroupId"] == new_group["id"]


def test_split_with_an_unknown_target_group_is_a_noop() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    before = m
    m = _call(
        ctx,
        "TG_splitWith({}, {}, {}, {})".format(_model_js(m), json.dumps("file:a.txt"), json.dumps("row"), json.dumps("no-such-group")),
    )
    assert m == before


def test_direction_locks_on_the_first_split_and_ignores_later_requests() -> None:
    ctx = _ctx()
    m = _init(ctx)
    for ref in ("a.txt", "b.txt", "c.txt"):
        m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", ref))))
    # First split: only one non-empty group exists, so "column" takes effect.
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("file:c.txt"), json.dumps("column")))
    assert m["direction"] == "column"
    # Second split requests "row" while 2 groups already exist: ignored.
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("file:b.txt"), json.dumps("row")))
    assert m["direction"] == "column", "direction must not change once 2+ groups exist"
    assert len(m["groups"]) == 3


def test_direction_unlocks_after_collapsing_back_to_one_group() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("session:s1"), json.dumps("column")))
    assert m["direction"] == "column"
    m = _call(ctx, "TG_closeTab({}, {})".format(_model_js(m), json.dumps("session:s1")))
    assert len(m["groups"]) == 1
    assert m["direction"] == "row"
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "b.txt"))))
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("file:b.txt"), json.dumps("column")))
    assert len(m["groups"]) == 2
    assert m["direction"] == "column", "the lock must release once back to a single group, so a fresh direction can be chosen again"


# ---------------------------------------------------------------------------
# focusGroup + activeDoc
# ---------------------------------------------------------------------------

def test_focus_group_changes_focus_without_touching_tabs() -> None:
    ctx = _ctx()
    m = _init(ctx)
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("file", "a.txt"))))
    m = _call(ctx, "TG_splitWith({}, {}, {})".format(_model_js(m), json.dumps("session:s1"), json.dumps("row")))
    other_group_id = [g["id"] for g in m["groups"] if g["id"] != m["focusedGroupId"]][0]
    before_groups = m["groups"]
    m = _call(ctx, f"TG_focusGroup({_model_js(m)}, {json.dumps(other_group_id)})")
    assert m["focusedGroupId"] == other_group_id
    assert m["groups"] == before_groups


def test_focus_group_is_a_noop_for_an_unknown_id() -> None:
    ctx = _ctx()
    m = _init(ctx)
    before = m["focusedGroupId"]
    m = _call(ctx, "TG_focusGroup({}, {})".format(_model_js(m), json.dumps("no-such-group")))
    assert m["focusedGroupId"] == before


def test_active_doc_reads_the_focused_groups_active_tab() -> None:
    ctx = _ctx()
    m = _init(ctx)
    assert _call(ctx, f"TG_activeDoc({_model_js(m)})") is None
    m = _call(ctx, "TG_openTab({}, {}, {{promote: true}})".format(_model_js(m), _model_js(_doc("session", "s1"))))
    doc = _call(ctx, f"TG_activeDoc({_model_js(m)})")
    assert doc == {"id": "session:s1", "kind": "session", "ref": "s1", "preview": False}
