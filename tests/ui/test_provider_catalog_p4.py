"""Platform wave P4: provider-catalog.jsx consumes what the P2/P3
backend waves built: reachable/unreachable status + error facts on PC_cardFor
(virgin rows status-less).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "provider-catalog.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _card_for_src() -> str:
    src = _src()
    start = src.index("function PC_cardFor(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def test_virgin_rows_render_no_reachability_badge() -> None:
    fn = _card_for_src()
    assert "last_probe_at != null" in fn or "last_probe_at == null" in fn


def test_reachable_and_unreachable_are_derived_from_last_probe_ok() -> None:
    fn = _card_for_src()
    assert '"reachable"' in fn and '"unreachable"' in fn
    assert "last_probe_ok" in fn


def test_card_shows_error_fact_row() -> None:
    fn = _card_for_src()
    assert "last_error" in fn


def test_error_row_only_renders_when_actually_unreachable() -> None:
    fn = _card_for_src()
    assert "!row.last_probe_ok" in fn or "row.last_probe_ok === false" in fn
    assert "last_error" in fn


def test_bundle_transpiles_with_provider_catalog_p4() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/provider-catalog.jsx === */" in text
