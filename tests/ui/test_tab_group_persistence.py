"""The tab groups survive a reload (console review C-023, 2026-10-08).

The URL names ONE document, so a reload used to keep only the active tab: three open sessions came back as one, and a split lost its second group. The
working set is now written to ``localStorage`` (per user, by identity only: a tab's kind and ref, its group, the active tab, the split direction) and read
back at the next load; the URL stays the source of truth for the active document.

``TG_serialize`` / ``TG_restore`` / ``TG_restoreInto`` (``ui/foundation/tab-group-model.js``) are pure and run in V8. ``TG_restore`` is the only thing that
reads what a browser stored, so it trusts none of it: anything that is not what ``TG_serialize`` wrote, or that would break the model's invariants (one tab
per kind and ref across all groups, at most one preview tab per group, a real active tab in each group, a real focused group), yields ``null`` or a repaired
model, never an exception and never a half-built one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "tab-group-model.js"
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
KINDS = ["session", "file", "diff", "wiki"]


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval("var window = globalThis;")
    c.eval(MODULE.read_text(encoding="utf-8"))
    try:
        yield c
    finally:
        c.close()


def _js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _model(groups: list[dict], direction: str = "row", focused: str | None = None) -> dict:
    return {"groups": groups, "direction": direction, "focusedGroupId": focused or groups[0]["id"]}


def _tab(kind: str, ref: str, preview: bool = False) -> dict:
    return {"id": f"{kind}:{ref}", "kind": kind, "ref": ref, "preview": preview}


def _group(gid: str, tabs: list[dict], active: str | None = None) -> dict:
    return {"id": gid, "tabs": tabs, "activeTabId": active if active is not None else (tabs[-1]["id"] if tabs else None)}


def _restore(ctx, saved) -> dict | None:
    return _js(ctx, f"TG_restore({json.dumps(saved)}, {json.dumps(KINDS)})")


def test_a_model_round_trips_with_its_layout(ctx) -> None:
    model = _model(
        [_group("g1", [_tab("session", "s1"), _tab("file", "a/b.txt", preview=True)], "session:s1"), _group("g2", [_tab("wiki", "home")])],
        direction="column", focused="g2",
    )
    restored = _restore(ctx, _js(ctx, f"TG_serialize({json.dumps(model)})"))
    assert restored == model


def test_only_the_identity_of_a_tab_is_written(ctx) -> None:
    """Ids and layout, nothing a session or a file could carry: no names, no content, no tab object beyond kind, ref and preview."""
    model = _model([_group("g1", [{**_tab("session", "s1"), "label": "secret name", "content": "x"}])])
    saved = _js(ctx, f"TG_serialize({json.dumps(model)})")
    assert set(saved) == {"v", "direction", "focused", "groups"}
    assert set(saved["groups"][0]) == {"id", "active", "tabs"}
    assert set(saved["groups"][0]["tabs"][0]) == {"kind", "ref", "preview"}
    assert "secret name" not in json.dumps(saved)


@pytest.mark.parametrize("junk", [
    None, "text", 3, [], {}, {"v": 2, "groups": []}, {"v": 1}, {"v": 1, "groups": "nope"}, {"v": 1, "groups": []},
    {"v": 1, "groups": [{"id": "g", "tabs": []}]},
    {"v": 1, "groups": [{"id": "g", "tabs": [{"kind": "session"}]}]},
    {"v": 1, "groups": [{"id": "g", "tabs": [{"kind": "session", "ref": ""}]}]},
    {"v": 1, "groups": [{"id": "g", "tabs": [{"kind": "unknown", "ref": "x"}]}]},
    {"v": 1, "groups": [{"id": "g", "tabs": [{"kind": "session", "ref": 5}]}]},
    {"v": 1, "groups": [None]},
])
def test_what_is_not_a_saved_model_restores_to_nothing(ctx, junk) -> None:
    assert _restore(ctx, junk) is None


def test_a_bad_tab_is_dropped_and_the_good_ones_kept(ctx) -> None:
    saved = {"v": 1, "direction": "row", "focused": "g", "groups": [{"id": "g", "active": "session:ok", "tabs": [
        {"kind": "session", "ref": "ok"}, {"kind": "nope", "ref": "x"}, {"kind": "file", "ref": ""}, None, {"kind": "file", "ref": "keep.txt"}]}]}
    restored = _restore(ctx, saved)
    assert [t["id"] for t in restored["groups"][0]["tabs"]] == ["session:ok", "file:keep.txt"]


def test_a_tab_open_in_two_groups_is_kept_once_in_the_first(ctx) -> None:
    saved = {"v": 1, "groups": [
        {"id": "g1", "tabs": [{"kind": "session", "ref": "s"}]}, {"id": "g2", "tabs": [{"kind": "session", "ref": "s"}, {"kind": "wiki", "ref": "w"}]}]}
    restored = _restore(ctx, saved)
    assert [[t["id"] for t in g["tabs"]] for g in restored["groups"]] == [["session:s"], ["wiki:w"]]


def test_a_group_has_at_most_one_preview_tab(ctx) -> None:
    saved = {"v": 1, "groups": [{"id": "g", "tabs": [
        {"kind": "file", "ref": "a", "preview": True}, {"kind": "file", "ref": "b", "preview": True}, {"kind": "file", "ref": "c", "preview": True}]}]}
    tabs = _restore(ctx, saved)["groups"][0]["tabs"]
    assert [t["preview"] for t in tabs] == [True, False, False]


def test_an_active_tab_that_is_not_in_its_group_falls_back_to_a_real_one(ctx) -> None:
    saved = {"v": 1, "groups": [{"id": "g", "active": "session:gone", "tabs": [{"kind": "session", "ref": "a"}, {"kind": "session", "ref": "b"}]}]}
    restored = _restore(ctx, saved)
    assert restored["groups"][0]["activeTabId"] == "session:b"


def test_a_focused_group_that_does_not_exist_falls_back_to_the_first(ctx) -> None:
    saved = {"v": 1, "focused": "ghost", "groups": [{"id": "g1", "tabs": [{"kind": "session", "ref": "a"}]}, {"id": "g2", "tabs": [{"kind": "session", "ref": "b"}]}]}
    assert _restore(ctx, saved)["focusedGroupId"] == "g1"


def test_an_empty_group_is_dropped_and_one_group_is_never_a_split(ctx) -> None:
    saved = {"v": 1, "direction": "column", "groups": [{"id": "g1", "tabs": []}, {"id": "g2", "tabs": [{"kind": "session", "ref": "a"}]}]}
    restored = _restore(ctx, saved)
    assert [g["id"] for g in restored["groups"]] == ["g2"] and restored["direction"] == "row"


def test_an_unknown_direction_is_a_row(ctx) -> None:
    saved = {"v": 1, "direction": "diagonal", "groups": [
        {"id": "g1", "tabs": [{"kind": "session", "ref": "a"}]}, {"id": "g2", "tabs": [{"kind": "session", "ref": "b"}]}]}
    assert _restore(ctx, saved)["direction"] == "row"


def test_the_restored_working_set_is_bounded(ctx) -> None:
    """A tampered or runaway store cannot make the shell open thousands of tabs: 60 tabs and 6 groups at most."""
    many = {"v": 1, "groups": [{"id": f"g{i}", "tabs": [{"kind": "session", "ref": f"s{i}-{j}"} for j in range(30)]} for i in range(10)]}
    restored = _restore(ctx, many)
    assert len(restored["groups"]) <= 6
    assert sum(len(g["tabs"]) for g in restored["groups"]) <= 60
    assert _restore(ctx, {"v": 1, "groups": [{"id": "g", "tabs": [{"kind": "session", "ref": "x" * 5000}]}]}) is None, "an absurd ref is not a tab"


def test_a_restored_group_id_is_reused_only_when_it_is_a_unique_string(ctx) -> None:
    saved = {"v": 1, "groups": [{"id": "dup", "tabs": [{"kind": "session", "ref": "a"}]}, {"id": "dup", "tabs": [{"kind": "session", "ref": "b"}]}, {"id": 7, "tabs": [{"kind": "session", "ref": "c"}]}]}
    ids = [g["id"] for g in _restore(ctx, saved)["groups"]]
    assert len(set(ids)) == 3 and all(isinstance(i, str) and i for i in ids)


def test_the_open_url_document_is_the_active_tab_after_a_restore_into_it(ctx) -> None:
    """The URL is the source of truth for the active document: the live model (the URL's doc, opened as a preview) is merged into the restored working set."""
    live = _js(ctx, 'TG_openTab(TG_init({groupId: "live"}), {kind: "session", ref: "from-url"}, {})')
    saved = {"v": 1, "focused": "g1", "groups": [{"id": "g1", "active": "session:old", "tabs": [{"kind": "session", "ref": "old"}, {"kind": "file", "ref": "keep.txt"}]}]}
    merged = _js(ctx, f"TG_restoreInto({json.dumps(live)}, {json.dumps(saved)}, {json.dumps(KINDS)})")
    ids = [t["id"] for g in merged["groups"] for t in g["tabs"]]
    assert set(ids) == {"session:old", "file:keep.txt", "session:from-url"}
    active = _js(ctx, f"TG_activeDoc({json.dumps(merged)})")
    assert (active["kind"], active["ref"]) == ("session", "from-url")


def test_restoring_into_a_live_model_with_nothing_saved_changes_nothing(ctx) -> None:
    live = _js(ctx, 'TG_openTab(TG_init({groupId: "live"}), {kind: "session", ref: "x"}, {})')
    assert _js(ctx, f"TG_restoreInto({json.dumps(live)}, null, {json.dumps(KINDS)})") == live
    assert _js(ctx, f'TG_restoreInto({json.dumps(live)}, {{"v": 9}}, {json.dumps(KINDS)})') == live


def test_a_url_doc_that_is_already_a_restored_tab_is_focused_not_duplicated(ctx) -> None:
    live = _js(ctx, 'TG_openTab(TG_init({groupId: "live"}), {kind: "session", ref: "b"}, {})')
    saved = {"v": 1, "groups": [{"id": "g1", "active": "session:a", "tabs": [{"kind": "session", "ref": "a"}, {"kind": "session", "ref": "b"}]}]}
    merged = _js(ctx, f"TG_restoreInto({json.dumps(live)}, {json.dumps(saved)}, {json.dumps(KINDS)})")
    assert [t["id"] for g in merged["groups"] for t in g["tabs"]] == ["session:a", "session:b"]
    assert _js(ctx, f"TG_activeDoc({json.dumps(merged)})")["ref"] == "b"


def test_the_shell_stores_per_user_only_after_it_has_restored() -> None:
    """Wiring pins: the key names the user (known once the auth status has loaded), the restore runs once and BEFORE the first write, and a corrupt
    stored value never breaks boot."""
    assert '"primer.console.tabs.v1:"' in SHELL
    assert "TG_restoreInto(" in SHELL and "TG_serialize(" in SHELL
    restore = SHELL.index("TG_restoreInto(")
    write = SHELL.index("TG_serialize(")
    assert restore < write, "the restore effect comes before the write effect"
    block = SHELL[SHELL.index("tabsStoreKey"):SHELL.index("// Menus close on any outside click.")]
    assert "tabsRestored" in block, "the write waits for the restore"
    helpers = SHELL[SHELL.index("function NV_loadTabs"):SHELL.index("function NV_Shell()")]
    assert helpers.count("try {") == 2 and helpers.count("catch") == 2, "storage can throw (private mode, quota) and hold anything: both accesses are guarded"
    assert "NV_loadTabs(tabsStoreKey)" in block and "NV_saveTabs(tabsStoreKey" in block
