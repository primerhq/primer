"""G10-4 (audit): the bootstrap progress panel desyncs during the backend's
``ingest_ai_docs`` phase.

The client's ``IC_BOOTSTRAP_PHASES`` hardcodes the phase list and is missing
that phase (the 7th of the 8-phase ``BootstrapPhase`` in
``primer/model/internal.py``), so ``findIndex`` answers -1, the bar regresses
to the first phase's position, and ``total`` is 7 against the 8 real phases,
inflating every displayed percentage. The list must match the backend enum in
order, the panel must read a known phase at its real position, and an unknown
phase id must render at its ordinal position with the raw id as its label,
never clamped to 0.

MiniRacer runs the panel's own computation (extracted from the source) over a
synthetic status row; the list-vs-enum check is a pure source scan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JSX = (ROOT / "ui" / "components" / "internal-collections.jsx").read_text(encoding="utf-8")
MODEL = (ROOT / "primer" / "model" / "internal.py").read_text(encoding="utf-8")


def _client_phases() -> list[str]:
    m = re.search(r"const IC_BOOTSTRAP_PHASES = \[.*?\];", JSX, re.S)
    assert m, "IC_BOOTSTRAP_PHASES not found"
    return re.findall(r'id: "([^"]+)"', m.group(0))


def _server_phases() -> list[str]:
    m = re.search(r"BootstrapPhase = Literal\[(.*?)\]", MODEL, re.S)
    assert m, "BootstrapPhase not found"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_the_client_phase_list_matches_the_backend_enum_in_order() -> None:
    assert _client_phases() == _server_phases()


def _panel(status_js: str) -> dict:
    """The panel's exact phase computation (its own lines, from the index line to the
    label line) run over a synthetic status row, in MiniRacer (scalar return)."""
    from py_mini_racer import MiniRacer

    m = re.search(r"const IC_BOOTSTRAP_PHASES = \[.*?\];", JSX, re.S)
    assert m, "IC_BOOTSTRAP_PHASES not found"
    phases_js = m.group(0).replace("const ", "var ", 1)
    panel = JSX[JSX.index("function BootstrapProgressPanel"):]
    m2 = re.search(r"const (?:foundIdx|phaseIndex) = .*?const currentLabel = .*?;", panel, re.S)
    assert m2, "the panel's phase computation not found"
    comp = re.sub(r"\bconst\b", "var", m2.group(0))
    ctx = MiniRacer()
    ctx.eval(phases_js)
    ctx.eval(f"var status = {status_js};")
    ctx.eval(comp)
    out = ctx.eval("JSON.stringify({ percent: percent, label: currentLabel })")
    ctx.close()
    return json.loads(out)


def test_the_panel_reads_ingest_ai_docs_at_its_real_position() -> None:
    """7th of the 8 real phases, 24% into it: the bar reads ~78%, named by its own label -
    not the first phase's 3%."""
    res = _panel('{"phase": "ingest_ai_docs", "phase_done": 120, "phase_total": 500}')
    assert 75 <= res["percent"] <= 88, res
    assert res["label"] == "Ingesting AI docs", res


def test_the_panel_never_clamps_an_unknown_phase_to_zero() -> None:
    """A phase id the client has not seen yet sits at its ordinal position (after the
    known ones) and is labelled by the raw id, never snapped to the first phase."""
    res = _panel('{"phase": "some_future_phase", "phase_done": 0, "phase_total": 0}')
    assert res["percent"] > 0, res
    assert res["label"] == "some_future_phase", res
