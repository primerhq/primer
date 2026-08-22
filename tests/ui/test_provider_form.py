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


# ---------------------------------------------------------------------------
# The form must send what it shows.
# ---------------------------------------------------------------------------


def _submittable_ctx():
    """MiniRacer with provider-form.jsx transpiled and loaded.

    PC_submittable is pure logic over a draft, so it is executed for real
    rather than substring-matched. React is stubbed because loading the
    module evaluates the component definitions, not their bodies.
    """
    from py_mini_racer import MiniRacer

    from primer.api._jsx_bundle import JSXBundler

    ui = UI
    bundler = JSXBundler(
        ui_dir=ui,
        babel_source=(ui / "vendor" / "babel.min.js").read_text(encoding="utf-8"),
    )
    code = bundler._transform(
        _read("components/provider-form.jsx"), "components/provider-form.jsx",
    )
    ctx = MiniRacer()
    ctx.eval(
        "var window = globalThis; window.primerApi = {};"
        "var React = { createElement: function () { return null; },"
        " useState: function (v) { return [v, function () {}]; },"
        " Fragment: null };"
    )
    ctx.eval(code)
    return ctx


def test_a_save_that_never_touched_limits_still_sends_limits() -> None:
    """Regression: the create 422'd with "limits: Field required".

    PC_LimitsFieldset renders ``value || { max_concurrency: 1 }``, a
    default that existed only inside that component's render. Filling the
    required fields and pressing Save therefore posted no ``limits`` key,
    and every provider class declares it required, so creating a provider
    through the catalog only worked if you happened to touch a Limits box
    on the way past.
    """
    ctx = _submittable_ctx()
    out = ctx.call(
        "PC_submittable",
        {"id": "llm-x", "provider": "anthropic", "config": {"api_key": "k"}},
        {"limits": True},
    )
    assert out["limits"] == {"max_concurrency": 1}, out


def test_limits_the_operator_did_set_win_over_the_default() -> None:
    ctx = _submittable_ctx()
    out = ctx.call(
        "PC_submittable",
        {"id": "llm-x", "limits": {"max_concurrency": 4}},
        {"limits": True},
    )
    assert out["limits"]["max_concurrency"] == 4


def test_a_class_without_limits_is_not_given_any() -> None:
    ctx = _submittable_ctx()
    out = ctx.call("PC_submittable", {"id": "ws-x"}, {})
    assert "limits" not in out


def test_emptied_number_boxes_are_dropped_rather_than_sent_as_blank() -> None:
    """"" fails float parsing server-side; a cleared optional box is unset."""
    ctx = _submittable_ctx()
    out = ctx.call(
        "PC_submittable",
        {"id": "llm-x", "config": {"port": "", "base_url": "http://x"}},
        {"config_fields": [{"key": "port", "type": "number"},
                           {"key": "base_url"}]},
    )
    assert "port" not in out["config"]
    assert out["config"]["base_url"] == "http://x"


def test_number_boxes_are_sent_as_numbers() -> None:
    ctx = _submittable_ctx()
    out = ctx.call(
        "PC_submittable",
        {"id": "llm-x", "config": {"port": "11434"}},
        {"config_fields": [{"key": "port", "type": "number"}]},
    )
    assert out["config"]["port"] == 11434


def test_a_save_that_never_touched_the_type_still_sends_one() -> None:
    """Same fault as limits: shown as a default, never written to the draft.

    ``selectedType`` falls back to the first key so the dropdown always
    shows something, but that fallback lived only in the render. Saving
    without touching the dropdown sent no ``provider``, which is required
    on every class, so the create 422'd while the form displayed a type.
    """
    ctx = _submittable_ctx()
    out = ctx.call("PC_submittable", {"id": "stt-x"}, {}, "whisper")
    assert out["provider"] == "whisper"


def test_a_type_the_operator_picked_wins_over_the_fallback() -> None:
    ctx = _submittable_ctx()
    out = ctx.call("PC_submittable", {"id": "stt-x", "provider": "deepgram"},
                   {}, "whisper")
    assert out["provider"] == "deepgram"


def test_save_is_withheld_while_a_required_field_is_empty() -> None:
    """The required marker beside the label has to mean something.

    A required field left empty is the same fault as a blank model row:
    the form knows the request cannot succeed and offered Save anyway, so
    the operator learned what was missing from a 422 rather than from the
    form in front of them. Speech providers hit this on every create,
    since default_model is required on the model and the field spec did
    not say so.
    """
    src = _read("components/provider-form.jsx")
    gate = src[src.index("const missingRequired ="):src.index("const modelRowsIncomplete")]
    assert "norm.required" in gate
    assert 'scope === "config"' in gate, (
        "config fields can be required too, not just row fields"
    )
    save = src[src.index('<Btn data-testid="provider-form-save"'):]
    save = save[:save.index("</Btn>")]
    assert "missingRequired()" in save


def test_the_speech_types_declare_the_fields_their_models_require() -> None:
    """_types is what the form learns required-ness from."""
    import asyncio

    from primer.api.routers.speech import list_stt_types, list_tts_types
    from primer.model.provider import (
        SpeechToTextProvider,
        TextToSpeechProvider,
    )

    for types_fn, model in (
        (list_stt_types, SpeechToTextProvider),
        (list_tts_types, TextToSpeechProvider),
    ):
        payload = asyncio.run(types_fn())
        config_model = model.model_fields["config"].annotation
        for spec in payload.values():
            for slot, owner in (("row_fields", model),
                                ("config_fields", config_model)):
                declared = {
                    f["key"] for f in spec[slot]
                    if isinstance(f, dict) and f.get("required")
                }
                listed = {
                    (g["key"] if isinstance(g, dict) else g)
                    for g in spec[slot]
                }
                needed = {
                    n for n, f in owner.model_fields.items()
                    if f.is_required() and n in listed
                }
                assert needed <= declared, (
                    f"{owner.__name__} requires {sorted(needed - declared)}, "
                    f"which the form is not told about via {slot}"
                )
