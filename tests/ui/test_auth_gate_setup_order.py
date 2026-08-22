"""S5 P2: the wizard gate sits between the password gate and the app."""
from __future__ import annotations

from pathlib import Path

AUTH = Path(__file__).resolve().parents[2] / "ui" / "components" / "auth.jsx"


def _gate() -> str:
    src = AUTH.read_text(encoding="utf-8")
    start = src.index("function AuthGate(")
    return src[start:src.index("function _AuthBrand(")]


def test_gate_consumes_the_setup_fact() -> None:
    assert "setup_complete" in _gate()


def test_wizard_is_admin_only_and_others_wait() -> None:
    gate = _gate()
    assert "SetupWizardGate" in gate
    assert "SetupWaitingScreen" in gate
    assert 'status.role === "admin"' in gate


def test_setup_gate_runs_after_the_password_gate_and_before_children() -> None:
    """Order the BRANCH, not the word.

    ``setup_complete`` also appears in the catch-branch fallback object at
    the top of the component, which precedes every gate; anchoring on the
    bare word would compare against that literal and pass vacuously. The
    branch reads ``status.setup_complete``, the fallback does not.
    """
    gate = _gate()
    assert gate.index("must_change_password") < gate.index("status.setup_complete")
    assert gate.index("status.setup_complete") < gate.index("return children")


def test_gate_failure_defaults_to_incomplete_setup() -> None:
    """The catch branch must not fabricate a complete install."""
    gate = _gate()
    assert "setup_complete: false" in gate
