from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "toolsets.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_sse_transport_chip_present() -> None:
    """The transport selector offers stdio, http, AND sse (legacy)."""
    src = _src()
    assert 'setTransport("sse")' in src
    assert 'transport === "sse"' in src


def test_submit_emits_selected_transport() -> None:
    """The non-stdio config build sends the SELECTED transport value (so
    'sse' is emitted), not a hardcoded 'http'."""
    src = _src()
    assert "transport: transport," in src


def test_initial_transport_restores_sse() -> None:
    """Editing an existing sse toolset restores the sse selection."""
    src = _src()
    assert '["http", "sse"].includes(existing?.config?.transport)' in src


def test_toolsets_jsx_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text()
    )
    code = b._transform(_src(), "components/toolsets.jsx")
    assert code and "transport" in code
