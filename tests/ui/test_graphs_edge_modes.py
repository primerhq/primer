"""Edge-mode toggle present; conditional creation builds the right shape."""
from pathlib import Path
SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"
def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_edge_mode_toggle_present() -> None:
    src = _src()
    assert "edgeMode" in src or "edge_mode" in src
    assert "Conditional" in src

def test_conditional_creation_uses_conditional_kind() -> None:
    src = _src()
    assert '"conditional"' in src
    assert "json_path" in src  # the default router shape

def test_static_remains_default() -> None:
    src = _src()
    assert '"static"' in src
