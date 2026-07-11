"""toolsets.jsx: TS_NewToolsetModal "Create anyway" escape hatch.

When POST /toolsets is rejected because the MCP endpoint is unreachable
(400 + problem type "/errors/toolset-unreachable"), the modal must surface
the message inline with a "Create anyway" button that re-submits the same
body with ?allow_unreachable=true (skipping the backend probe).

Static-source checks + a transpile gate (the tests/ui convention — no
DOM/browser harness).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TOOLSETS = UI / "components" / "toolsets.jsx"


def _src() -> str:
    return TOOLSETS.read_text(encoding="utf-8")


def _modal_src() -> str:
    src = _src()
    start = src.index("function TS_NewToolsetModal(")
    end = src.index("function TS_KvEditor(", start)
    return src[start:end]


def test_onerror_detects_the_unreachable_contract_type_uri() -> None:
    # Single shared contract with the backend reject: the dedicated type URI.
    modal = _modal_src()
    assert 'err.type === "/errors/toolset-unreachable"' in modal


def test_create_anyway_button_present() -> None:
    modal = _modal_src()
    assert 'data-testid="toolset-create-anyway"' in modal
    assert "Create anyway" in modal


def test_inline_unreachable_block_rendered() -> None:
    modal = _modal_src()
    assert 'data-testid="toolset-unreachable"' in modal


def test_bypass_flag_appends_allow_unreachable_query() -> None:
    modal = _modal_src()
    assert 'allowUnreachable ? "?allow_unreachable=true" : ""' in modal


def test_create_anyway_resubmits_same_body_with_bypass() -> None:
    modal = _modal_src()
    # The click handler re-submits the remembered body with the bypass flag.
    assert "create.mutate({ body: lastBody, allowUnreachable: true })" in modal
    # The normal submit path stores the body and sends without the bypass.
    assert "setLastBody(body)" in modal
    assert "create.mutate({ body, allowUnreachable: false })" in modal


def test_bundle_transpiles_with_create_anyway_flow() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/toolsets.jsx === */" in text
