"""The live-stream frame renderer + coalescing helpers live in a shared
module so the graph node inspector reuses them verbatim. Must load
before session-detail.jsx and the bundle must still transpile."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
FRAME = UI / "components" / "session-frame.jsx"
DETAIL = UI / "components" / "session-detail.jsx"
INDEX = UI / "index.html"


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_frame_module_exists_and_exports() -> None:
    src = FRAME.read_text(encoding="utf-8")
    assert "function _SLS_Frame" in src
    assert "function _SLS_coalesceMessages" in src
    assert "window._SLS_Frame" in src or "_SLS_Frame," in src


def test_frame_definitions_removed_from_detail() -> None:
    src = DETAIL.read_text(encoding="utf-8")
    assert "function _SLS_Frame(" not in src
    assert "function _SLS_coalesceMessages(" not in src


def test_frame_registered_before_detail() -> None:
    order = _order()
    assert "components/session-frame.jsx" in order
    assert order.index("components/session-frame.jsx") < order.index(
        "components/session-detail.jsx"
    )


def test_bundle_transpiles_with_frame_module() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/session-frame.jsx === */" in text
