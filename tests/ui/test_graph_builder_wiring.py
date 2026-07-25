"""Graph-builder wiring: every new file transpiles through the real bundler,
is registered in index.html in dependency order, and the shell is reachable
from GraphDetail behind the graphBuilderV2 tweak (ui/graph-builder/WIRING.md
§1, §12, §14, §15).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
GB = UI / "components" / "graph-builder"
INDEX = UI / "index.html"
GRAPHS = UI / "components" / "graphs.jsx"
CANVAS = UI / "components" / "graph-canvas.jsx"
PICKER = UI / "components" / "shared" / "entity-picker.jsx"

GB_FILES = [
    "gb-model.jsx", "gb-api.jsx", "gb-refs.jsx", "gb-validate.jsx", "gb-canvas.jsx",
    "gb-outline.jsx", "gb-palette.jsx", "gb-schema.jsx", "gb-ref-editor.jsx",
    "gb-branches.jsx", "gb-inspector.jsx", "gb-readiness.jsx", "gb-dryrun.jsx",
    "gb-starters.jsx", "graph-builder.jsx",
]


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            out.append(line[start:line.index('"', start)])
    return out


def test_every_builder_file_exists() -> None:
    for name in GB_FILES:
        assert (GB / name).exists(), f"ui/components/graph-builder/{name} is missing"
    # EntityPicker is the shipped shared component (WIRING.md §2); the builder
    # reuses it rather than growing a second picker.
    assert PICKER.exists()


def test_all_files_transpile() -> None:
    # The no-build console pre-transpiles server-side; a syntax error here is a
    # blank page in production.
    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text(encoding="utf-8")
    )
    for rel in [f"components/graph-builder/{n}" for n in GB_FILES] + [
        "components/graph-canvas.jsx",
        "components/graphs.jsx",
    ]:
        src = (UI / rel).read_text(encoding="utf-8")
        code = bundler._transform(src, rel)
        assert code, f"{rel} transpiled to nothing"


def test_registered_in_index_in_dependency_order() -> None:
    order = _order()
    for name in GB_FILES:
        assert f"components/graph-builder/{name}" in order, f"{name} not registered in index.html"
    # Model/refs/validate must load before the components that call them, the
    # whole builder before graphs.jsx (which renders GB_Builder), and the
    # picker before the builder that uses it.
    idx = {p: i for i, p in enumerate(order)}
    assert idx["components/graph-builder/gb-model.jsx"] < idx["components/graph-builder/gb-canvas.jsx"]
    assert idx["components/graph-builder/gb-refs.jsx"] < idx["components/graph-builder/gb-ref-editor.jsx"]
    assert idx["components/graph-builder/gb-validate.jsx"] < idx["components/graph-builder/graph-builder.jsx"]
    assert idx["components/graph-builder/graph-builder.jsx"] < idx["components/graphs.jsx"]
    assert idx["components/shared/entity-picker.jsx"] < idx["components/graph-builder/gb-palette.jsx"]
    assert idx["components/graph-canvas.jsx"] < idx["components/graph-builder/gb-canvas.jsx"]


def test_graph_detail_renders_the_new_builder_behind_the_tweak() -> None:
    src = GRAPHS.read_text(encoding="utf-8")
    assert "window.GB_Builder" in src, "GraphDetail must render the new builder"
    assert "graphBuilderV2" in src, "the swap must be behind the tweak"
    # The old editor stays reachable, and is still the default until the
    # ui_e2e journeys are migrated to the new surface.
    assert "<GR_GraphEditor" in src
    tweaks = (UI / "foundation" / "tweaks.js").read_text(encoding="utf-8")
    assert "graphBuilderV2:" in tweaks, "the revamp must be behind a named tweak"


def test_canvas_labels_lead_with_the_human_name() -> None:
    # Known-failure #2: generated ids leaked into the visual language.
    src = CANVAS.read_text(encoding="utf-8")
    assert "node.description || node.id" in src
    assert "function _g6Ref(" in src, "cards show what the step runs on a second line"


def test_canvas_rejects_the_illegal_fanout_gesture() -> None:
    # Known-failure #5: drawing an edge out of a fan-out is a validation error,
    # so it must be refused at the gesture with an explanation.
    src = CANVAS.read_text(encoding="utf-8")
    assert 'kind === "fan_out"' in src
    assert "onIllegalEdge" in src
    assert 'kind === "begin"' in src


def test_canvas_draws_tee_fanout_targets() -> None:
    # tee targets live on target_node_ids; without this they were invisible.
    src = CANVAS.read_text(encoding="utf-8")
    assert "sp.target_node_ids" in src


def test_read_only_mode_for_harness_managed_graphs() -> None:
    src = (GB / "graph-builder.jsx").read_text(encoding="utf-8")
    assert "harness_id" in src
    assert "readOnly" in src
    assert "managed by harness" in src


def test_testid_contract() -> None:
    # §14 - the existing HTML test harness keys off these.
    blob = "\n".join((GB / n).read_text(encoding="utf-8") for n in GB_FILES)
    for tid in (
        "gb-builder", "gb-topbar", "gb-save", "gb-dirty", "gb-json-tab",
        "gb-readiness-chip", "gb-readiness-popover", "gb-readiness-item", "gb-readiness-fix",
        "gb-outline", "gb-outline-row", "gb-outline-add",
        "gb-palette", "gb-palette-search", "gb-palette-row",
        "gb-canvas", "gb-fanout-bracket", "gb-band",
        "gb-inspector", "gb-inspector-title", "gb-agent-picker", "gb-tool-picker",
        "gb-ref-editor", "gb-ref-chip", "gb-ref-picker", "gb-ref-row",
        "gb-schema-row", "gb-schema-json-toggle",
        "gb-branch", "gb-branch-op", "gb-branch-catchall",
        "gb-dryrun", "gb-dryrun-row", "gb-dryrun-run",
        "gb-starters", "gb-starter",
    ):
        assert f'"{tid}"' in blob, f"missing data-testid: {tid}"


def test_only_gb_api_calls_the_api() -> None:
    """§2: gb-api.jsx owns the API surface.

    Declarative `path="/agents"` props on EntityPicker are the prescribed
    pattern (§2) and are fine; what must not appear elsewhere is a direct
    apiFetch call or a hand-built request URL.
    """
    for name in GB_FILES:
        if name == "gb-api.jsx":
            continue
        src = (GB / name).read_text(encoding="utf-8")
        assert "apiFetch(" not in src, f"{name} calls apiFetch directly; route it through gb-api.jsx"
        for fragment in ('"/v1/', "`/graphs/", "`/agents/", "`/sessions"):
            assert fragment not in src, f"{name} builds a URL directly; route it through gb-api.jsx"


def test_starters_are_valid_topologies() -> None:
    """Each starter must satisfy the persist-time tier and, once its agent
    placeholders are filled, the runnable tier too."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {};")
    for f in ("gb-model.jsx", "gb-refs.jsx", "gb-validate.jsx"):
        ctx.eval((GB / f).read_text(encoding="utf-8"))
    # gb-starters.jsx contains JSX, so evaluate only its data + helper.
    src = (GB / "gb-starters.jsx").read_text(encoding="utf-8")
    start = src.index("const GB_STARTERS")
    end = src.index("function GB_Starters(")
    ctx.eval(src[start:end])

    ctx.eval(
        """
        var report = [];
        for (var i = 0; i < GB_STARTERS.length; i++) {
          var s = GB_STARTERS[i];
          var picks = {};
          for (var j = 0; j < s.slots.length; j++) picks[s.slots[j].key] = "chosen_" + j;
          var filled = GB_fillStarter(s.spec, picks);
          var v = GB_validate(filled, {});
          report.push({shape: s.shape,
                       blocking: v.blocking.map(function(b){return b.code}).join(","),
                       runnable: v.runnable.map(function(b){return b.code}).join(",")});
        }
        """
    )
    n = ctx.eval("report.length")
    assert n == 6
    for i in range(n):
        shape = ctx.eval(f"report[{i}].shape")
        blocking = ctx.eval(f"report[{i}].blocking")
        runnable = ctx.eval(f"report[{i}].runnable")
        assert blocking == "", f"starter {shape!r} has persist-time violations: {blocking}"
        assert runnable == "", f"starter {shape!r} is not runnable once filled: {runnable}"
