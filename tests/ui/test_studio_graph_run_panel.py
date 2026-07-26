"""Studio revamp: the graph run strip and the phone layout (WIRING §10, §11).

The superstep grouping is derived, not served, so it is the part that can be
silently wrong - a strip that drops the un-run nodes claims the graph is smaller
than it is, and one that paints a pending segment as live is worse than no strip.
Both are exercised for real in MiniRacer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
GRAPH = UI / "components" / "studio" / "st-graph-run.jsx"
MOBILE = UI / "components" / "studio" / "st-mobile.jsx"
STUDIO = UI / "components" / "studio.jsx"


def _code_only(src: str) -> str:
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def _ctx():
    """MiniRacer with the pure superstep derivation loaded (no React)."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    src = GRAPH.read_text(encoding="utf-8")
    start = src.index("function ST2_nodeBucket(status)")
    end = src.index("// ---------------------------------------------------------------------------\n// Rendering")
    ctx.eval(src[start:end])
    return ctx


def _steps(ctx, items):
    ctx.eval(f"var out = ST2_supersteps({json.dumps(items)});")
    return json.loads(ctx.eval("JSON.stringify(out)"))


# ---------------------------------------------------------------------------
# The endpoint carries no superstep field - pin that the derivation is honest
# ---------------------------------------------------------------------------


def test_node_states_really_has_no_superstep_field() -> None:
    # WIRING §10 says "one segment per superstep". The endpoint's model has no
    # such field, so the strip derives from `iteration`. If a superstep field is
    # ever added, this test fails and the derivation should be replaced by it.
    from primer.api.routers.compute import _NodeStateOut

    fields = set(_NodeStateOut.model_fields)
    assert "superstep" not in fields
    assert "iteration" in fields
    assert fields == {
        "node_id", "kind", "status", "iteration", "last_run_at",
        "tokens_in", "tokens_out", "duration_ms", "error",
    }


def test_node_status_vocabulary_matches_the_backend() -> None:
    # The five statuses the route can emit (compute.py docstring), each of which
    # ST2_nodeBucket must map deliberately.
    ctx = _ctx()
    got = {
        status: ctx.eval(f'ST2_nodeBucket("{status}")')
        for status in ("pending", "running", "completed", "failed", "skipped")
    }
    assert got == {
        "pending": "pending",
        "running": "working",
        "completed": "done",
        "failed": "broken",
        "skipped": "done",
    }


def test_pending_stays_distinct_from_working() -> None:
    # Folding "not started" into "working" would paint an idle segment as live,
    # which is the one thing the strip exists to tell you.
    ctx = _ctx()
    assert ctx.eval('ST2_nodeBucket("pending")') != ctx.eval('ST2_nodeBucket("running")')


def test_an_unknown_status_reads_as_pending_not_done() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_nodeBucket("something-new")') == "pending"
    assert ctx.eval("ST2_nodeBucket(null)") == "pending"


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_supersteps_group_by_iteration_in_order() -> None:
    steps = _steps(_ctx(), [
        {"node_id": "c", "status": "pending", "iteration": 2},
        {"node_id": "a", "status": "completed", "iteration": 0},
        {"node_id": "b", "status": "running", "iteration": 1},
    ])
    assert [s["index"] for s in steps] == [0, 1, 2]


def test_iterations_sort_numerically_not_lexically() -> None:
    # Keyed through an object, so "10" would sort before "2" as a string.
    steps = _steps(_ctx(), [
        {"node_id": "a", "status": "completed", "iteration": 10},
        {"node_id": "b", "status": "completed", "iteration": 2},
    ])
    assert [s["index"] for s in steps] == [2, 10]


def test_a_fan_out_becomes_one_segment_carrying_its_width() -> None:
    steps = _steps(_ctx(), [
        {"node_id": "fan", "status": "completed", "iteration": 1},
        {"node_id": "fan", "status": "completed", "iteration": 1},
        {"node_id": "fan", "status": "running", "iteration": 1},
    ])
    assert len(steps) == 1
    assert steps[0]["width"] == 3
    assert steps[0]["label"] == "fan"


def test_the_caption_shows_the_multiplier_only_when_it_fanned_out() -> None:
    ctx = _ctx()
    ctx.eval('var one = ST2_stepCaption({index: 0, label: "n", width: 1});')
    ctx.eval('var many = ST2_stepCaption({index: 1, label: "fan", width: 4});')
    assert ctx.eval("one") == "0 n"
    assert ctx.eval("many") == "1 fan x4"


def test_not_yet_run_nodes_become_a_trailing_upcoming_segment() -> None:
    # They carry no iteration, so they cannot be placed in a numbered step -
    # but dropping them would make the strip claim a shorter graph.
    steps = _steps(_ctx(), [
        {"node_id": "a", "status": "completed", "iteration": 0},
        {"node_id": "b", "status": "pending"},
        {"node_id": "c", "status": "pending", "iteration": None},
    ])
    assert len(steps) == 2
    assert steps[-1]["upcoming"] is True
    assert steps[-1]["index"] is None
    assert steps[-1]["width"] == 2
    assert steps[-1]["bucket"] == "pending"


def test_no_upcoming_segment_when_every_node_has_run() -> None:
    steps = _steps(_ctx(), [{"node_id": "a", "status": "completed", "iteration": 0}])
    assert len(steps) == 1
    assert steps[0]["upcoming"] is False


def test_an_empty_run_yields_no_segments() -> None:
    assert _steps(_ctx(), []) == []
    ctx = _ctx()
    ctx.eval("var out = ST2_supersteps(null);")
    assert ctx.eval("out.length") == 0


def test_iteration_zero_is_a_real_superstep_not_a_falsy_miss() -> None:
    # `if (!it)` here would send every first superstep into "upcoming".
    steps = _steps(_ctx(), [{"node_id": "a", "status": "completed", "iteration": 0}])
    assert steps[0]["index"] == 0
    assert steps[0]["upcoming"] is False


# ---------------------------------------------------------------------------
# A step is as bad as its worst node
# ---------------------------------------------------------------------------


def test_one_failure_defines_the_segment() -> None:
    # Nine siblings completing does not make the step fine; the failure is what
    # the operator has to act on.
    ctx = _ctx()
    items = [{"node_id": "f", "status": "completed", "iteration": 0} for _ in range(9)]
    items.append({"node_id": "f", "status": "failed", "iteration": 0})
    steps = _steps(ctx, items)
    assert steps[0]["bucket"] == "broken"


def test_running_beats_pending_and_done() -> None:
    ctx = _ctx()
    steps = _steps(ctx, [
        {"node_id": "a", "status": "completed", "iteration": 0},
        {"node_id": "a", "status": "running", "iteration": 0},
        {"node_id": "a", "status": "pending", "iteration": 0},
    ])
    assert steps[0]["bucket"] == "working"


def test_a_failure_beats_a_running_sibling() -> None:
    ctx = _ctx()
    steps = _steps(ctx, [
        {"node_id": "a", "status": "running", "iteration": 0},
        {"node_id": "a", "status": "failed", "iteration": 0},
    ])
    assert steps[0]["bucket"] == "broken"


def test_all_completed_is_done() -> None:
    steps = _steps(_ctx(), [
        {"node_id": "a", "status": "completed", "iteration": 0},
        {"node_id": "b", "status": "skipped", "iteration": 0},
    ])
    assert steps[0]["bucket"] == "done"


def test_a_skipped_node_names_its_exclusion_without_inventing_one() -> None:
    ctx = _ctx()
    ctx.eval('var withReason = ST2_skipReason({status: "skipped", error: "findings == 0"});')
    ctx.eval('var bare = ST2_skipReason({status: "skipped"});')
    ctx.eval('var notSkipped = ST2_skipReason({status: "completed"});')
    assert "findings == 0" in ctx.eval("withReason")
    assert ctx.eval("bare") == "condition false"
    assert ctx.eval("notSkipped") is None


# ---------------------------------------------------------------------------
# Rendering + wiring
# ---------------------------------------------------------------------------


def test_the_strip_is_dom_chrome_never_a_second_canvas() -> None:
    src = GRAPH.read_text(encoding="utf-8")
    assert '"superstep-strip"' in src
    code = _code_only(src)
    for banned in ("GR_Canvas", "new G6", "window.G6"):
        assert banned not in code, banned


def test_segments_are_weighted_by_node_count() -> None:
    src = GRAPH.read_text(encoding="utf-8")
    assert 'flex: step.width + " 1 0"' in src


def test_only_the_live_segment_sweeps() -> None:
    src = GRAPH.read_text(encoding="utf-8")
    assert 'var live = step.bucket === "working";' in src
    assert '"superstep-sweep"' in src


def test_the_sweep_class_exists_and_reuses_the_shipped_keyframes() -> None:
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert ".st-sweep {" in css
    assert "animation: shimmer" in css
    # One sweep animation, not two.
    assert css.count("@keyframes shimmer") == 1


def test_the_node_states_fetch_never_runs_with_a_null_key() -> None:
    # useResource has no null-key guard: a null key composes to "null", creates
    # a cache entry, and fires GET /graphs/null/runs/null/node_states.
    src = GRAPH.read_text(encoding="utf-8")
    assert "function ST2_RunSteps({ gid, rid, onPickNode })" in src
    assert "{gid && rid ? <ST2_RunSteps" in src
    assert "ST2_api.keys.nodeStates(gid, rid)" in src


def test_the_node_states_cache_key_exists() -> None:
    api = (UI / "components" / "studio" / "st-api.jsx").read_text(encoding="utf-8")
    assert "nodeStates: function (gid, rid)" in api


def test_the_lane_inspector_is_the_companion_pane_not_a_drawer() -> None:
    src = GRAPH.read_text(encoding="utf-8")
    assert "studio.openAside" in src
    # And it degrades to the shipped in-place node filter when there is no pane.
    assert "onFallback" in src


def test_graph_run_module_does_not_name_urls() -> None:
    src = GRAPH.read_text(encoding="utf-8")
    assert "/graphs/" not in _code_only(src)
    assert "ST2_api.nodeStates" in src


# ---------------------------------------------------------------------------
# Mobile (§11)
# ---------------------------------------------------------------------------


def test_the_bottom_bar_keeps_the_shipped_mobile_testids() -> None:
    # The drawers are gone, but the shipped journeys locate the bar by these.
    src = MOBILE.read_text(encoding="utf-8")
    assert '"studio-left-toggle"' in src
    assert '"studio-right-toggle"' in src


def test_bottom_bar_items_meet_the_touch_target_minimum() -> None:
    src = MOBILE.read_text(encoding="utf-8")
    assert "minHeight: 44" in src


def test_the_phone_layout_is_triage_not_a_shrunk_desktop() -> None:
    src = _code_only(MOBILE.read_text(encoding="utf-8"))
    # No editor, no terminal, no event feed on a phone.
    for banned in ("TerminalPanel", "FilePanel", "WorkspaceTap", "FilesTree", "InvestigateDock"):
        assert banned not in src, banned
    assert "mobile-desktop-only" in src


def test_suggestion_chips_are_offered_for_questions_only() -> None:
    # A one-tap "Yes" on an approval would authorise an arbitrary command.
    src = MOBILE.read_text(encoding="utf-8")
    assert 'item.kind === "ask_user" ? (' in src
    assert "ST2_ASK_SUGGESTIONS" in src
    assert '"mobile-suggestions"' in src


def test_mobile_reuses_the_shared_yield_controls_and_actions() -> None:
    src = MOBILE.read_text(encoding="utf-8")
    assert "<ST2_YieldControls item={item} actions={actions} />" in src
    assert "ST2_useYieldActions(wid)" in src
    # Not a second implementation of the write path.
    assert "useMutation" not in src
    assert "apiFetch" not in src


def test_mobile_runs_reuse_the_bucket_language_and_sort() -> None:
    src = MOBILE.read_text(encoding="utf-8")
    assert "ST2_bucketOf(s, { pendingBySession: pendingBySession })" in src
    assert "ST_sessionSort(sessions || [])" in src


def test_the_desktop_body_is_not_rendered_on_a_phone() -> None:
    # A hidden terminal / file editor would still open its websockets.
    src = STUDIO.read_text(encoding="utf-8")
    assert "isV2 && isMobileView && typeof window.StudioMobile" in src
    assert "window.primerApi.useViewport().isMobile" in src


def test_both_new_modules_transpile_and_are_registered() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    index = (UI / "index.html").read_text(encoding="utf-8")
    for rel in ("components/studio/st-graph-run.jsx", "components/studio/st-mobile.jsx",
                "components/studio.jsx"):
        assert b._transform((UI / rel).read_text(encoding="utf-8"), rel), rel
    assert "studio/st-graph-run.jsx" in index
    assert "studio/st-mobile.jsx" in index
