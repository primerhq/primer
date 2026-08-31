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


def test_r5_globals_are_exported() -> None:
    src = _src()
    assert "window.SetupPredicatesList = SetupPredicatesList" in src
    assert "window.NV_SetupPage = NV_SetupPage" in src


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
    """The wizard reuses the auth-shell system; every class it adds on top
    (the original 2-class step sequence, R5's 6-class predicate list)
    must exist in styles.css or something renders unstyled."""
    css = (UI / "styles.css").read_text(encoding="utf-8")
    for rule in (
        ".setup-steps", ".setup-progress", ".auth-field select",
        ".setup-predicates", ".setup-predicate", ".setup-predicate-dot",
        ".setup-predicate-label", ".setup-predicate-detail", ".setup-predicate-fix",
    ):
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


def test_predicates_list_reads_the_live_state_endpoint() -> None:
    """R5 BUILD: the six-predicate checklist is fed by GET /setup/state
    (live-checked), not GET /auth/status (presence-only, stays cheap for
    its hot unauthenticated-probe role)."""
    src = _src()
    assert '"/setup/state"' in src


def test_predicates_list_renders_ok_and_fix_action_per_row() -> None:
    src = _src()
    start = src.index("function SetupPredicatesList(")
    end = src.index("\nconst _CAPABILITY_GATES", start)
    body = src[start:end]
    assert "state.predicates.map" in body
    assert "p.ok" in body
    assert "p.detail" in body
    assert "Configure provider" in body
    assert "Re-run seed" in body


def test_fetch_capabilities_hits_the_capabilities_endpoint() -> None:
    src = _src()
    start = src.index("function _fetchCapabilities(")
    end = src.index("\n}", start)
    body = src[start:end]
    assert '"/capabilities"' in body


def test_admin_setup_page_has_predicates_capabilities_and_reset_actions() -> None:
    """R5 BUILD: NV_SetupPage is the whole Setup admin surface - six
    predicates + capabilities table + Re-run seed + Reset base agent
    roster (the latter two REUSE - no backend change needed)."""
    src = _src()
    start = src.index("function NV_SetupPage(")
    end = src.index("\n// ====", start)
    body = src[start:end]
    assert "SetupPredicatesList" in body
    assert "_fetchCapabilities()" in body
    assert "nv-sys-setup-rerun-seed" in body
    assert '"/setup/seed"' in body
    assert "nv-sys-setup-reset-roster" in body
    assert '"/setup/reset_agents"' in body
    assert "nv-sys-capabilities-table" in body


def test_admin_setup_page_configure_provider_reuses_the_wizard_steps() -> None:
    """The provider/profile predicates can't be auto-seeded (notes: they
    need real operator input) - their fix-action reopens the existing
    2-step SetupWizardSteps inline rather than duplicating that form."""
    src = _src()
    start = src.index("function NV_SetupPage(")
    end = src.index("\n// ====", start)
    body = src[start:end]
    assert "<SetupWizardSteps" in body


def test_gate_requires_all_predicates_before_entering() -> None:
    """notes section 5: "Enter Primer" enables only when all six pass."""
    src = _src()
    host = src[src.index("function SetupWizardGate("):src.index("function SetupWaitingScreen(")]
    assert "setup-gate-enter" in host
    assert "disabled={!state || !state.complete}" in host


def test_gate_routes_returning_admins_past_a_finished_provider_step() -> None:
    """A returning admin whose provider/profile already exist (e.g. a
    prior ensure pass failed on the workspace backend) must land on the
    predicate checklist, not be forced through the 2-step form again -
    the routing decision reads state.predicates, not just state.complete."""
    src = _src()
    start = src.index("function SetupWizardGate(")
    end = src.index("function SetupWaitingScreen(")
    body = src[start:end]
    assert "providerMissing" in body
    assert "llm_provider" in body and "model_profile" in body
