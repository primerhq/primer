"""Task F2 of docs/superpowers/plans/2026-07-05-chat-refactor.md — the
structured-output schema side panel's Builder + JSON tabs (R3/§8.3),
filling in the Task B4 <SchemaPanel> shell
(ui/components/shared/schema-panel.jsx) and wiring persistent/ephemeral
application into <Conversation> (ui/components/chat/conversation.jsx).

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_composer_schema_shells.py / test_turn_anatomy.py) — no
DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
SHARED_DIR = UI / "components" / "shared"
SCHEMA_PANEL = SHARED_DIR / "schema-panel.jsx"
CONVERSATION = CHAT_DIR / "conversation.jsx"


def _panel_src() -> str:
    return SCHEMA_PANEL.read_text(encoding="utf-8")


def _conv_src() -> str:
    return CONVERSATION.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# JSON tab — live validation (valid JSON AND a valid-looking JSON Schema)
# ---------------------------------------------------------------------------


def test_json_tab_parses_and_reports_invalid_json() -> None:
    src = _panel_src()
    assert "JSON.parse(text)" in src
    # Invalid JSON must flip validity to false without touching onChange.
    assert "onValidityChange(false)" in src


def test_json_tab_runs_a_structural_schema_check_beyond_bare_json_parse() -> None:
    src = _panel_src()
    assert "function SP_validateSchemaStructure(" in src
    assert "structural.ok" in src
    # Sanity-checks top-level type/properties/required shape per the plan.
    assert '"properties" must be an object' in src
    assert "SP_JSON_SCHEMA_TYPES" in src


def test_json_source_of_truth_only_propagates_on_success() -> None:
    src = _panel_src()
    # emit()/onValidityChange(true) only follow the success path — the
    # catch/structural-fail branches return before reaching them.
    assert "const handleJsonTextChange = (text) => {" in src
    assert "emit(parsed)" in src
    assert "onValidityChange(true)" in src


# ---------------------------------------------------------------------------
# Invalid -> blocks invocation (schemaInvalid gate, already reserved on
# <Composer> since Task B4 — this just needs to actually flip it).
# ---------------------------------------------------------------------------


def test_invalid_state_surfaces_an_inline_banner() -> None:
    src = _panel_src()
    assert 'valid === false' in src
    assert 'data-testid="schema-invalid-banner"' in src
    assert "send disabled" in src


# ---------------------------------------------------------------------------
# Builder tab — subset (§8.3): flat + nested objects/arrays, scalar types,
# required, enum, with a graceful "edit in JSON" escape.
# ---------------------------------------------------------------------------


def test_builder_supports_scalar_types_object_and_array() -> None:
    src = _panel_src()
    assert 'const SP_SCALAR_TYPES = ["string", "number", "integer", "boolean"];' in src
    assert '"object"' in src
    assert '"array"' in src


def test_builder_field_editor_is_recursive_for_nested_objects_and_arrays() -> None:
    src = _panel_src()
    assert "function SP_FieldsEditor(" in src
    # Object fields recurse into another SP_FieldsEditor for their children;
    # array-of-object fields recurse into one for their item's children.
    assert "field.children" in src
    assert "field.itemChildren" in src
    assert src.count("<SP_FieldsEditor") >= 2


def test_builder_supports_required_and_enum() -> None:
    src = _panel_src()
    assert 'data-testid="schema-builder-field-required"' in src
    assert "SP_parseEnumInput" in src
    assert "SP_enumToInput" in src


def test_builder_has_graceful_edit_in_json_escape() -> None:
    src = _panel_src()
    assert "function SP_isRepresentableNode(" in src
    assert "function SP_isRepresentableSchema(" in src
    assert 'data-testid="schema-builder-escape"' in src
    assert 'data-testid="schema-builder-edit-in-json"' in src
    assert 'setTab("json")' in src


def test_builder_edits_regenerate_the_json_tab() -> None:
    src = _panel_src()
    assert "const handleBuilderFieldsChange = (nextFields) => {" in src
    assert "SP_fieldsToSchema(nextFields)" in src
    assert "setJsonText(JSON.stringify(schema, null, 2))" in src


def test_json_edits_rehydrate_the_builder_when_representable() -> None:
    src = _panel_src()
    assert "function SP_schemaToFields(" in src
    assert "setBuilderFields(SP_schemaToFields(parsed))" in src
    assert "setBuilderEscape(true)" in src


# ---------------------------------------------------------------------------
# Persistent toggle: ON -> PUT /chats/{id}/response_format; OFF -> ephemeral
# on the next user_message send frame only.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Panel stays a pure, controlled shell — no data fetching/WS of its own
# (regression guard against the persistence logic leaking into it).
# ---------------------------------------------------------------------------


def test_schema_panel_still_pure_no_data_fetching_or_ws() -> None:
    src = _panel_src()
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


# ---------------------------------------------------------------------------
# studio-ux fix 2: the "⚙ schema" chip (chats.jsx) toggles the panel OPEN in
# ONE click — no more internal collapsed-rail intermediate step between the
# chip mounting <SchemaPanel> and the operator seeing the Builder/JSON tabs.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# studio-ux fix 3: a chat with no response_format override of its own still
# shows the EFFECTIVE (agent build-time) schema, labeled as inherited and
# read-only until the operator explicitly starts an override.
# ---------------------------------------------------------------------------


def test_schema_panel_accepts_inherited_schema_prop() -> None:
    src = _panel_src()
    assert "inheritedSchema = null," in src


def test_schema_panel_shows_inherited_only_without_a_chat_override() -> None:
    src = _panel_src()
    assert "const hasOwnValue = value !== null && value !== undefined;" in src
    assert "const showingInherited = !hasOwnValue && inheritedSchema != null && !overriding;" in src


def test_schema_panel_inherited_view_is_labeled_and_read_only() -> None:
    src = _panel_src()
    assert 'data-testid="schema-inherited-banner"' in src
    assert "Inherited from agent" in src
    assert 'data-testid="schema-inherited-preview"' in src
    assert "readOnly" in src
    assert 'data-testid="schema-inherited-edit"' in src


def test_schema_panel_inherited_seed_effect_never_emits_before_an_edit() -> None:
    # The seeding effect's CODE (not its explanatory comment, which
    # discusses emit()/onChange by name) must only set local display state
    # (setJsonText/setBuilderFields) — never actually call emit() or
    # onChange — so opening the panel on an agent with a default schema
    # can't silently create a per-chat override.
    src = _panel_src()
    start = src.index("React.useEffect(() => {\n    if (hasOwnValue || overriding || inheritedSchema == null) return;")
    end = src.index("const emit = (schema) => {")
    seed_effect = src[start:end]
    assert "emit(" not in seed_effect
    assert "onChange(" not in seed_effect
    assert "setJsonText(JSON.stringify(inheritedSchema, null, 2));" in seed_effect


def test_schema_panel_edit_override_button_unlocks_editing() -> None:
    src = _panel_src()
    assert "onClick={() => setOverriding(true)}" in src


