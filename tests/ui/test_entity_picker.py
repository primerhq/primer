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
NEW_SESSION = UI / "components" / "new-session-form.jsx"
CHATS = UI / "components" / "chats.jsx"
INDEX = UI / "index.html"


def _picker_src() -> str:
    return PICKER.read_text(encoding="utf-8")


def _new_session_src() -> str:
    return NEW_SESSION.read_text(encoding="utf-8")


def _chats_src() -> str:
    return CHATS.read_text(encoding="utf-8")


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
    for consumer in ("components/new-session-form.jsx", "components/chats.jsx"):
        assert order.index(consumer) > picker_at, f"{consumer} loads before entity-picker.jsx"


# ---- new-session-form.jsx wires the picker, not a bare <select> -----------


def _agent_graph_field_block(src: str) -> str:
    start = src.index('{kind === "agent" ? "Agent" : "Graph"}')
    end = src.index("noBinding &&", start)
    return src[start:end]


def test_new_session_form_uses_entity_picker_for_agent_and_graph() -> None:
    block = _agent_graph_field_block(_new_session_src())
    assert block.count("<EntityPicker") == 2
    assert 'path="/agents"' in block
    assert 'path="/graphs"' in block
    assert "<select" not in block, "agent/graph binding should use EntityPicker, not a bare <select>"


def test_new_session_form_picker_wired_to_existing_state() -> None:
    block = _agent_graph_field_block(_new_session_src())
    assert "value={agentId}" in block and "onChange={setAgentId}" in block
    assert "value={graphId}" in block and "onChange={setGraphId}" in block


def test_new_session_form_still_resolves_selected_graph_for_begin_schema() -> None:
    # The graph binding drives a dependent Begin.input_schema dynamic form
    # (selectedGraph lookup); dropping the select must not drop this.
    src = _new_session_src()
    assert "selectedGraph" in src
    assert "graphItems.find" in src
    assert "input_schema" in src and "graph_input" in src


# ---- chats.jsx "New chat" creator wires the same picker --------------------


def test_new_chat_modal_uses_entity_picker() -> None:
    src = _chats_src()
    start = src.index("function CT_NewChatModal(")
    end = src.index("\nfunction ", start + 1)
    block = src[start:end]
    assert "<EntityPicker" in block
    assert 'path="/agents"' in block
    assert "<select" not in block, "New chat agent binding should use EntityPicker, not a bare <select>"


# ---- Transpile checks -------------------------------------------------------


def test_entity_picker_jsx_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_picker_src(), "components/shared/entity-picker.jsx")
    assert code and "EntityPicker" in code


def test_new_session_form_jsx_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_new_session_src(), "components/new-session-form.jsx")
    assert code and "EntityPicker" in code


def test_chats_jsx_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_chats_src(), "components/chats.jsx")
    assert code and "EntityPicker" in code


def test_bundle_transpiles_with_entity_picker() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/shared/entity-picker.jsx === */" in text
    assert "function EntityPicker(" in text
