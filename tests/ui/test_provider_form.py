"""One parameterized form replaces the per-class form duplicates.

S4 section 6: EXTRA_FOR_PROVIDER_TYPE capability hints render INSIDE the
shared form, and ui/foundation/capabilities.js survives unchanged.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_form_file_is_registered_before_the_catalog() -> None:
    html = _read("index.html")
    assert 'src="components/provider-form.jsx"' in html
    assert html.index("components/provider-form.jsx") < html.index(
        "components/provider-catalog.jsx"
    ), "the catalog renders the form, so the form must load first"


def test_the_form_exports_itself_on_window() -> None:
    src = _read("components/provider-form.jsx")
    assert "window.PC_ProviderForm = PC_ProviderForm;" in src


def test_the_form_is_driven_by_the_types_endpoint_not_a_hardcoded_table() -> None:
    src = _read("components/provider-form.jsx")
    assert "_types" in src
    assert "config_fields" in src
    assert "row_fields" in src


def test_capability_hints_render_inside_the_shared_form() -> None:
    src = _read("components/provider-form.jsx")
    assert "EXTRA_FOR_PROVIDER_TYPE" in src
    assert "capabilityHint" in src
    assert "useCapabilities" in src


def test_the_capabilities_foundation_module_is_untouched() -> None:
    """S4 section 6 pins this: capabilities.js survives unchanged."""
    src = _read("foundation/capabilities.js")
    assert "ns.EXTRA_FOR_PROVIDER_TYPE = EXTRA_FOR_PROVIDER_TYPE;" in src
    assert src.count("EXTRA_FOR_PROVIDER_TYPE") == 3


def test_secret_fields_render_as_password_inputs() -> None:
    src = _read("components/provider-form.jsx")
    assert '"api_key": "password"' in src or "api_key: \"password\"" in src


def test_the_form_offers_the_test_round_trip() -> None:
    src = _read("components/provider-form.jsx")
    assert "onTest" in src
    assert '"/_test"' in src or "/_test" in src
    assert "data-testid=\"provider-form-test\"" in src


def test_the_catalog_renders_the_shared_form_for_every_crud_class() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "PC_ProviderForm" in src


def test_no_per_class_form_duplicate_survives_in_the_catalog() -> None:
    """The whole point of the parameterized form: exactly one place
    knows how to render a provider config."""
    src = _read("components/provider-catalog.jsx")
    assert "config_fields" not in src, (
        "field rendering belongs in provider-form.jsx, not the catalog shell"
    )
