"""Searchable + paginated agent/graph picker (EntityPicker).

Replaces the old "GET /agents?limit=200 dumped into a <select>" pattern in
the New session form (and the New chat creator) with a reusable component
that searches server-side via the `?q=` ILIKE support added alongside this
UI change (see primer/api's list-endpoint search), paged through the
existing shared `usePagedList` + `Pager` primitive (tests/ui/test_pagination.py).

Static-source + bundle-build checks only (matching the rest of the ui/
suite -- no React render).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PICKER = UI / "components" / "shared" / "entity-picker.jsx"
INDEX = UI / "index.html"


def _picker_src() -> str:
    return PICKER.read_text(encoding="utf-8")



# ---- The component exists + is defined -------------------------------------


def test_picker_file_exists() -> None:
    assert PICKER.exists()


def test_component_defined_and_exported() -> None:
    src = _picker_src()
    assert "function EntityPicker(" in src
    # Bare global + primerApi namespace, mirroring shared/pager.jsx's idiom.
    assert "window.EntityPicker = EntityPicker" in src
    assert "ns.EntityPicker = EntityPicker" in src


# ---- Uses usePagedList with a server-side `q` param ------------------------


def test_uses_paged_list_hook() -> None:
    src = _picker_src()
    assert "usePagedList(" in src


def test_search_text_is_debounced_before_becoming_q() -> None:
    src = _picker_src()
    # A raw input value is held separately from the debounced `q` that is
    # actually sent as a query param, via setTimeout/clearTimeout.
    assert "setTimeout(" in src
    assert "clearTimeout(" in src
    assert "params: q ?" in src or "params:q?" in src.replace(" ", "")
    assert "resetKey: q" in src or "resetKey:q" in src.replace(" ", "")


def test_search_input_present() -> None:
    src = _picker_src()
    assert 'name="search"' in src
    assert "input-icon" in src


def test_pager_rendered() -> None:
    assert "<Pager" in _picker_src()


def test_selection_clear_control_present() -> None:
    src = _picker_src()
    assert "Selected:" in src
    assert "onChange(\"\")" in src


# ---- Registered in the bundle, in the right order --------------------------


def _bundle_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_registered_in_index() -> None:
    assert "components/shared/entity-picker.jsx" in _bundle_order()


def test_loads_after_pager_before_consumers() -> None:
    order = _bundle_order()
    picker_at = order.index("components/shared/entity-picker.jsx")
    assert picker_at > order.index("components/shared.jsx")
    assert picker_at > order.index("components/shared/pager.jsx")
    for consumer in ("components/graph-builder/gb-palette.jsx", "components/graph-builder/gb-inspector.jsx"):
        assert order.index(consumer) > picker_at, f"{consumer} loads before entity-picker.jsx"


# ---- Transpile checks -------------------------------------------------------


def test_entity_picker_jsx_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_picker_src(), "components/shared/entity-picker.jsx")
    assert code and "EntityPicker" in code


def test_bundle_transpiles_with_entity_picker() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/shared/entity-picker.jsx === */" in text
    assert "function EntityPicker(" in text
