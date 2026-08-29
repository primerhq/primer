"""Platform wave P4: semantic-search.jsx kills its three hardcoded SSP
kind enums in favor of GET /ssp/_types (the same data source the LLM/
embedding/cross-encoder register dropdowns already use), collapses the
advanced HNSW/DiskANN/vector-type knobs behind a closed-by-default
<details> ("minimal SSP form" - the fields /ssp/_types itself doesn't
describe stay real and settable, just not part of the default view),
and wires test-connect for both a draft config (POST /ssp/_test) and a
saved row (GET /ssp/{id}/_test).

Static-source checks only (the tests/ui suite convention).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "semantic-search.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _fn_block(start_marker: str, end_marker: str) -> str:
    src = _src()
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _list_src() -> str:
    return _fn_block("function SSPListPage(", "function BackendBadge(")


def _create_modal_src() -> str:
    return _fn_block("function SSPCreateModal(", "function FieldRow(")


def _detail_src() -> str:
    return _fn_block("function SSPDetail(", "function SSPOverview(")


# ---- Hardcoded enum killed in all three spots -----------------------------


def test_filter_dropdown_is_sourced_from_ssp_types() -> None:
    list_src = _list_src()
    assert '"GET", "/ssp/_types"' in list_src
    assert 'sspKinds.map((k) =>' in list_src
    assert '<option value="pgvector">pgvector</option>' not in list_src


def test_filter_dropdown_no_longer_omits_lance() -> None:
    """The old hardcoded pair didn't even include lance - a real,
    pre-existing gap this fixes for free by sourcing from the same
    served data instead of hand-maintaining a second enum copy."""
    list_src = _list_src()
    assert '"pgvectorscale">pgvectorscale</option>' not in list_src


def test_create_modal_backend_picker_is_sourced_from_ssp_types() -> None:
    modal = _create_modal_src()
    assert '"GET", "/ssp/_types"' in modal
    assert "sspKinds.map((k) =>" in modal
    assert '<option value="lance">' not in modal


def test_lance_capability_hint_is_still_wired_separately() -> None:
    """The lance-missing hint is a runtime extras check
    (useCapabilities), not a form-shape fact /ssp/_types describes -
    killing the hardcoded enum must not also kill this."""
    modal = _create_modal_src()
    assert "lanceMissing" in modal
    assert "not installed" in modal


def test_backend_badge_distinguishes_all_three_real_kinds() -> None:
    """RETARGET: the old two-way color map lumped pgvectorscale and
    lance into one "not pgvector" bucket."""
    src = _src()
    assert "SSP_BADGE_COLORS" in src
    badge_start = src.index("const SSP_BADGE_COLORS")
    badge_end = src.index("};", badge_start)
    colors = src[badge_start:badge_end]
    assert "pgvector:" in colors
    assert "pgvectorscale:" in colors
    assert "lance:" in colors


# ---- Minimal form: advanced knobs collapsed, not deleted -------------------


def test_advanced_knobs_are_collapsed_by_default() -> None:
    """/ssp/_types' own docstring (routers/semantic_search.py's
    _postgres_family_fields) calls these advanced tuning knobs with sane
    defaults, deliberately left off the minimal form contract. Collapsed
    behind a <details> (closed on create, open on edit - see
    test_advanced_details_open_state below), not deleted: every field
    is real, working, currently-shipped configurability."""
    modal = _create_modal_src()
    assert '<details data-testid="ssp-advanced-details"' in modal
    assert "HNSW knobs" in modal
    assert "DiskANN" in modal
    assert "Vector type" in modal
    assert 'FieldRow label="schema"' in modal


def test_advanced_details_open_state_matches_edit_vs_create() -> None:
    """Closed by default on create (minimal-by-default); open on edit,
    since a saved row's real advanced values should be visible, not
    hidden behind an extra click."""
    modal = _create_modal_src()
    details_line = modal[modal.index("<details"):modal.index(">", modal.index("<details")) + 1]
    assert "open={isEdit}" in details_line


def test_opening_advanced_is_a_one_way_ratchet() -> None:
    """Once opened, advancedOpened latches true - it must not flip back
    to false on close, or an edit made while the panel was open would
    silently vanish from the submit body if the operator collapsed it
    again before saving."""
    modal = _create_modal_src()
    assert "onToggle={(e) => { if (e.target.open) setAdvancedOpened(true); }}" in modal


def test_edit_mode_always_includes_advanced_fields() -> None:
    """A saved row's real values must round-trip regardless of whether
    the operator re-opens Advanced during this edit session."""
    modal = _create_modal_src()
    assert "React.useState(isEdit)" in modal


# ---- Test-connect: draft ----------------------------------------------------


def test_config_builder_is_shared_between_submit_and_test_connect() -> None:
    """One place builds the provider-shaped config from form state,
    used by both the real Save body and the draft test-connect probe -
    so the probe always tests exactly what Save would submit."""
    src = _src()
    assert "function _sspBuildConfig(form, isLance, isScale, includeAdvanced)" in src
    modal = _create_modal_src()
    assert "_sspBuildConfig(form, isLance, isScale, advancedOpened)" in modal
    test_box_start = src.index("function SSP_TestConnectBox(")
    test_box_end = src.index("\nfunction ", test_box_start + 1)
    test_box = src[test_box_start:test_box_end]
    assert "_sspBuildConfig(form, isLance, isScale, includeAdvanced)" in test_box


def test_config_builder_omits_advanced_keys_when_not_included() -> None:
    """An untouched create must submit config_fields only (hostname/
    port/username/password/database, or path for lance) - matching the
    backend's confirmed core-fields-only-create case (the union fix
    Dev-Backend pinned for pgvectorscale specifically)."""
    src = _src()
    fn_start = src.index("function _sspBuildConfig(")
    fn_end = src.index("\n}", src.index("\n}", fn_start) + 1)
    fn_src = src[fn_start:fn_end]
    assert "if (includeAdvanced) {" in fn_src
    # db_schema, hnsw_m and enable_diskann must all live inside that gate.
    gate_start = fn_src.rindex("if (includeAdvanced) {")
    gate_body = fn_src[gate_start:]
    assert "config.db_schema" in gate_body
    assert "config.hnsw_m" in gate_body
    assert "config.enable_diskann" in gate_body


def test_draft_test_connect_posts_to_ssp_test() -> None:
    src = _src()
    test_box_start = src.index("function SSP_TestConnectBox(")
    test_box_end = src.index("\nfunction ", test_box_start + 1)
    test_box = src[test_box_start:test_box_end]
    assert '"POST", "/ssp/_test"' in test_box
    assert 'data-testid="ssp-test-connect-draft-run"' in test_box


def test_draft_test_connect_shows_the_mandated_reachability_only_copy() -> None:
    """Exact copy: SSP reports reachability only, no model list (unlike
    the LLM/embedding families' live probe panel)."""
    src = _src()
    test_box_start = src.index("function SSP_TestConnectBox(")
    test_box_end = src.index("\nfunction ", test_box_start + 1)
    test_box = src[test_box_start:test_box_end]
    assert "test-connect available — this kind reports reachability, not a model list." in test_box
    assert "Test connection" in test_box


def test_draft_test_connect_is_mounted_in_the_create_modal() -> None:
    modal = _create_modal_src()
    assert "<SSP_TestConnectBox form={form} isLance={isLance} includeAdvanced={advancedOpened} />" in modal


# ---- Register dropdown routes SSP creation --------------------------------


def test_ssp_creation_is_routed_through_the_shared_register_dropdown() -> None:
    """'like every other family' - reuses PC_RegisterDropdown
    (provider-catalog.jsx) rather than a bespoke New-provider button, so
    kind decides the form up front for SSP too."""
    list_src = _list_src()
    assert '<PC_RegisterDropdown klass={{ plural: "ssp" }} onPick={openCreate} />' in list_src
    assert "New provider" not in list_src
    assert "New Semantic Search provider" not in list_src


def test_picked_kind_seeds_the_create_modals_initial_provider() -> None:
    list_src = _list_src()
    assert "initialProvider={createKind}" in list_src
    modal = _create_modal_src()
    assert "provider: initialProvider || \"pgvector\"" in modal


# ---- Test-connect: saved ----------------------------------------------------


def test_saved_test_connect_gets_the_unredacted_stored_config() -> None:
    """GET /ssp/{id}/_test reads the stored row server-side - a client-
    held GET would carry the SecretStr's "**********" mask, per that
    route's own docstring, so the saved test-connect must hit this
    endpoint rather than resubmitting the (redacted) detail data."""
    detail = _detail_src()
    assert '"GET", `/ssp/${encodeURIComponent(sspId)}/_test`' in detail
    assert 'data-testid="ssp-test-connect-saved-run"' in detail


def test_saved_test_connect_result_renders_ok_or_error() -> None:
    detail = _detail_src()
    assert 'data-testid="ssp-test-connect-saved-ok"' in detail
    assert 'data-testid="ssp-test-connect-saved-error"' in detail


def test_bundle_transpiles_with_semantic_search_p4() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/semantic-search.jsx === */" in text
