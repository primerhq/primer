"""The shared form renders what _types serves, and nothing it invents.

Task 28 moved the provider field table into the backend. This pins that
the form reads descriptors, still accepts the bare-name shape web_search
and the speech classes answer with, and can produce a row the backend
will actually accept (models[] and limits are required).
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _src() -> str:
    return (UI / "components" / "provider-form.jsx").read_text(encoding="utf-8")


def test_bare_names_and_descriptors_are_both_accepted() -> None:
    """web_search/_types answers ["api_key"]; llm_providers/_types answers
    [{key: "api_key", ...}]. One renderer, normalised once."""
    src = _src()
    assert "function PC_normalizeField(" in src
    assert 'typeof field === "string"' in src


def test_enum_fields_render_a_select_over_the_served_options() -> None:
    src = _src()
    assert 'field.type === "enum"' in src
    assert "field.options" in src


def test_required_help_and_placeholder_reach_the_markup() -> None:
    src = _src()
    assert "field.required" in src
    assert "field.help" in src
    assert "field.placeholder" in src


def test_the_models_row_field_has_a_real_editor() -> None:
    """embedding and cross-encoder rows are rejected with zero models
    (models: min_length=1), so a bare text box makes the class unusable."""
    src = _src()
    assert 'field.type === "model_list"' in src
    assert "function PC_ModelListField(" in src
    assert 'data-testid="provider-form-add-model"' in src


def test_limits_are_editable_when_the_class_declares_them() -> None:
    """max_concurrency has no default on Limits, so a form that never
    sends limits can only ever produce a 422."""
    src = _src()
    assert "function PC_LimitsFieldset(" in src
    assert "max_concurrency" in src
    assert "request_timeout_seconds" in src
    assert "shape.limits" in src


def test_no_provider_field_table_survives_in_the_console() -> None:
    """The point of _types: exactly one place knows what a provider type
    needs, and it is the place that owns the enums."""
    src = _src()
    for token in (
        "openresponses",
        "openrouter",
        "huggingface",
        "text-embedding-3-small",
        "lmstudio",
    ):
        assert token not in src, (
            f"{token!r} in provider-form.jsx re-creates the table _types serves"
        )


def test_the_type_picker_shows_the_served_label() -> None:
    src = _src()
    assert "shapeOf(" in src or "typeMap[k]" in src
    assert ".label" in src
