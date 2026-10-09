"""The legacy graph editor and its ``graphBuilderV2`` tweak are gone (lead ruling 2026-10-09).

``GR_GraphEditor`` and the helpers only it used (most of ``graphs.jsx``) were the fallback the live ``GB_Builder`` replaced: ``GraphDetail`` rendered it only when the ``graphBuilderV2`` tweak was ``false``, and
nothing in the UI sets that tweak. Dead code is not harmless here: it sat in every bundle and held most of the bare form labels the ratchet counts in ``graphs.jsx``. This is the guard that it stays
deleted, and that the pieces the builder and the graphs list still use are still there and still used.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
GRAPHS = UI / "components" / "graphs.jsx"

RETIRED = (
    "GR_GraphEditor", "GR_DD_ITEM_STYLE", "GR_ViolationsBanner", "GR_localViolations", "GR_TextField", "GR_TextAreaField", "GR_NumberField", "GR_SidePanel", "GR_GraphFields",
    "GR_GraphStats", "GR_SelectedNodeForm", "GR_ON_FAILURE_OPTS", "GR_FanOutSpecsEditor", "GR_ToolCallForm", "GR_SelectedEdgeForm", "GR_BRANCH_OPS", "GR_formatBranchValue",
    "GR_BranchEditor", "GR_EdgeOutRow",
)
KEPT = (
    "GraphsPage", "GraphDetail", "GR_GraphStatusPanel", "GR_NewGraphModal", "GR_ImportSpecModal", "GR_JsonField", "GR_parseBranchValue", "GR_stripCoords",
)


def _ui_sources() -> dict[Path, str]:
    return {
        p: p.read_text(encoding="utf-8")
        for p in sorted(UI.rglob("*"))
        if p.suffix in {".jsx", ".js", ".html"} and "vendor" not in p.relative_to(UI).parts
    }


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


@pytest.mark.parametrize("name", RETIRED)
def test_a_retired_name_is_nowhere_in_the_ui(name: str) -> None:
    where = [str(p.relative_to(UI)) for p, text in _ui_sources().items() if _word(name).search(text)]
    assert not where, f"{name} was retired with the legacy graph editor but is still named in: {where}"


def test_the_builder_tweak_is_gone() -> None:
    for word in ("graphBuilderV2", "useBuilderV2"):
        where = [str(p.relative_to(UI)) for p, text in _ui_sources().items() if word in text]
        assert not where, f"{word} is still named in: {where}"


def test_graphs_jsx_defines_only_the_pieces_that_are_still_used() -> None:
    defined = set(re.findall(r"^(?:function|const|let|var)\s+(\w+)", GRAPHS.read_text(encoding="utf-8"), re.M))
    assert defined == set(KEPT), f"extra: {sorted(defined - set(KEPT))}, missing: {sorted(set(KEPT) - defined)}"


@pytest.mark.parametrize("name", KEPT)
def test_every_kept_piece_is_used_outside_its_own_definition(name: str) -> None:
    uses = sum(len(_word(name).findall(text)) for text in _ui_sources().values())
    assert uses >= 2, f"{name} is defined and never used: delete it"


def test_graph_detail_renders_the_builder_and_reads_no_tweak() -> None:
    text = GRAPHS.read_text(encoding="utf-8")
    body = text.split("function GraphDetail(", 1)[1].split("\n}\n", 1)[0]
    assert "<window.GB_Builder" in body
    assert "useTweaks" not in body and "grTweaks" not in body


def test_no_helper_graphs_jsx_names_is_left_undefined() -> None:
    sources = _ui_sources()
    defined = {m for text in sources.values() for m in re.findall(r"(?:function|const|let|var)\s+(GR_\w+)", text)}
    used = set(re.findall(r"\bGR_\w+", GRAPHS.read_text(encoding="utf-8")))
    assert used <= defined, f"graphs.jsx names a helper nothing defines: {sorted(used - defined)}"
