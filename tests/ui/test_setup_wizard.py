"""S5 P2: the bootstrap wizard is an embeddable, no-chrome step sequence."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "setup-wizard.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_three_globals_are_exported() -> None:
    src = _src()
    assert "window.SetupWizardSteps = SetupWizardSteps" in src
    assert "window.SetupWizardGate = SetupWizardGate" in src
    assert "window.SetupWaitingScreen = SetupWaitingScreen" in src


def test_step_sequence_has_no_chrome_and_no_navigation() -> None:
    """C5 mount contract: S8 re-hosts SetupWizardSteps verbatim, so it must
    not touch the console shell, routes, or the address bar."""
    src = _src()
    start = src.index("function SetupWizardSteps(")
    end = src.index("function SetupWizardGate(")
    body = src[start:end]
    assert "window.location" not in body
    assert "ROUTES" not in body
    assert "auth-shell" not in body


def test_step_one_probes_the_draft_provider() -> None:
    src = _src()
    assert '"/llm_providers/_discover_models"' in src
    assert '"/llm_providers"' in src


def test_step_two_reuses_the_probe_result_and_creates_a_profile() -> None:
    """M11e: the same _discover_models response is step 2's model list."""
    src = _src()
    assert '"/model_profiles"' in src
    assert "discovered" in src
    assert "context_length" in src


def test_provider_is_created_before_the_profile() -> None:
    src = _src()
    assert src.index('"/llm_providers"') < src.index('"/model_profiles"')


def test_limits_block_is_posted_with_the_provider() -> None:
    assert "max_concurrency" in _src()


def test_wizard_is_loaded_by_the_console() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'src="components/setup-wizard.jsx"' in html


def test_every_class_the_wizard_introduces_is_defined() -> None:
    """The wizard reuses the auth-shell system and adds exactly two classes;
    both must exist in styles.css or the step sequence renders unstyled."""
    css = (UI / "styles.css").read_text(encoding="utf-8")
    for rule in (".setup-steps", ".setup-progress", ".auth-field select"):
        assert rule in css, rule


def test_wizard_transpiles_via_the_server_bundler() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text(),
    )
    code = b._transform(_src(), "components/setup-wizard.jsx")
    assert code and "SetupWizardSteps" in code

def test_completion_invokes_the_seed_endpoint() -> None:
    """C3: the wizard explicitly runs the ensure pass once a profile exists."""
    src = _src()
    host = src[src.index("function SetupWizardGate("):src.index("function SetupWaitingScreen(")]
    assert '"/setup/seed"' in host
