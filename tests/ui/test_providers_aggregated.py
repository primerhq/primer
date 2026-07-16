from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "providers.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_aggregated_provider_type_registered() -> None:
    src = _src()
    assert "aggregated:" in src          # new key under PROVIDER_KINDS_FIELDS.llm
    assert "Aggregated" in src           # human label


def test_aggregated_member_editor_and_controls_present() -> None:
    src = _src()
    assert "PR_AggregatedEditor" in src
    assert "PR_Toggle" in src
    # Strategy / failover-point toggles use the role="switch" idiom.
    assert 'role="switch"' in src
    # Enum string values must match the backend StrEnums exactly.
    for token in ("round_robin", "sequential", "before_first_token",
                  "mid_stream", "transient", "transient_and_config"):
        assert token in src
    # Member shape fields.
    assert "provider_id" in src and "model_name" in src


def test_providers_jsx_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_src(), "components/providers.jsx")
    assert code and "PROVIDER_KINDS_FIELDS" in code
