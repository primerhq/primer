"""The aggregated LLM editor, ported out of providers.jsx.

Same assertions the deleted tests/ui/test_providers_aggregated.py made,
against the module that now owns the editor: an ordered member picker
plus the routing and failover switches, with enum strings that match the
backend StrEnums exactly.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "provider-aggregated-editor.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_editor_and_toggle_are_present() -> None:
    src = _src()
    assert "function PC_AggregatedEditor(" in src
    assert "function PC_Toggle(" in src
    assert 'role="switch"' in src


def test_enum_strings_match_the_backend_stringenums() -> None:
    src = _src()
    for token in (
        "round_robin",
        "sequential",
        "before_first_token",
        "mid_stream",
        "transient",
        "transient_and_config",
    ):
        assert token in src, f"{token} missing; RoutingStrategy/FailoverPoint/FailoverClasses"


def test_member_shape_is_a_provider_model_pair() -> None:
    """A member pins a (provider, model) pair, not a profile id: the
    aggregated adapter dispatches to the upstream directly."""
    src = _src()
    assert "provider_id" in src and "model_name" in src


def test_members_are_reorderable() -> None:
    """Order is the failover order, so it has to be editable."""
    src = _src()
    assert "const move =" in src


def test_defaults_mirror_the_backend_field_defaults() -> None:
    src = _src()
    assert "PC_AGG_CONFIG_DEFAULT" in src
    for token in ("sequential", "before_first_token", "transient_and_config"):
        assert token in src


def test_it_exports_itself_on_window() -> None:
    src = _src()
    assert "window.PC_AggregatedEditor = PC_AggregatedEditor;" in src


def test_the_form_mounts_it_for_the_aggregated_variant_only() -> None:
    form = (UI / "components" / "provider-form.jsx").read_text(encoding="utf-8")
    assert 'shape.variant === "aggregated"' in form
    assert "PC_AggregatedMount" in form


def test_nested_aggregation_is_not_offered() -> None:
    """An aggregated member must resolve to a real upstream adapter;
    nesting is rejected server-side at resolve time."""
    form = (UI / "components" / "provider-form.jsx").read_text(encoding="utf-8")
    assert 'p.provider !== "aggregated"' in form


def test_the_file_is_registered_before_the_form() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'src="components/provider-aggregated-editor.jsx"' in html
    assert html.index("components/provider-aggregated-editor.jsx") < html.index(
        "components/provider-form.jsx"
    )


def test_it_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text()
    )
    code = b._transform(_src(), "components/provider-aggregated-editor.jsx")
    assert code and "PC_AggregatedEditor" in code
