"""S5 P2: the console's gate chain, pinned at the source level.

Static-source, not Playwright: tests/ui_e2e is collect-ignored without
PRIMER_RUN_UI_E2E=1, and amendment M16 defers journey depth for shell
surfaces to S8. What must not drift before then is the ORDER:
register/login -> forced password change -> restricted -> setup wizard ->
app.
"""
from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
AUTH = UI / "components" / "auth.jsx"
WIZARD = UI / "components" / "setup-wizard.jsx"


def _gate() -> str:
    src = AUTH.read_text(encoding="utf-8")
    return src[src.index("function AuthGate("):src.index("function _AuthBrand(")]


def test_the_gates_appear_in_order() -> None:
    gate = _gate()
    order = [
        gate.index("must_change_password"),
        gate.index('status.role === "restricted"'),
        gate.index("status.setup_complete"),
        gate.index("return children"),
    ]
    assert order == sorted(order), order
    assert "RegisterScreen" in gate and "LoginScreen" in gate


def test_non_admins_wait_instead_of_seeing_the_wizard() -> None:
    gate = _gate()
    wizard_at = gate.index("SetupWizardGate")
    waiting_at = gate.index("SetupWaitingScreen")
    assert gate.index('status.role === "admin"') < wizard_at
    assert wizard_at < waiting_at


def test_the_wizard_lands_the_user_back_in_the_app() -> None:
    """Completion exits the gate the way every other gate does: full reload."""
    src = WIZARD.read_text(encoding="utf-8")
    host = src[
        src.index("function SetupWizardGate("):
        src.index("function SetupWaitingScreen(")
    ]
    assert "onDone" in host
    # Only the HOST reloads; the embeddable step sequence never does.
    assert "window.location.reload" in _gate()
