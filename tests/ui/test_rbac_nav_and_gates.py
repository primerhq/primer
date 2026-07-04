"""Static + transpile checks for role-gated nav + restricted / must-change
AuthGate screens (RBAC console, Spec §6/§12).

Nav gating is cosmetic — the server enforces RBAC on every route (Task 7).
These checks pin the console surface the e2e suite + manual smoke rely on.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "ui" / "components" / "auth.jsx"
CHROME = ROOT / "ui" / "components" / "chrome.jsx"
APP = ROOT / "ui" / "app.jsx"


def test_pending_access_screen_defined() -> None:
    src = AUTH.read_text()
    assert "ADM_PendingAccessScreen" in src
    assert "window.ADM_PendingAccessScreen" in src


def test_must_change_password_screen_defined() -> None:
    src = AUTH.read_text()
    assert "ADM_MustChangePasswordScreen" in src
    assert "window.ADM_MustChangePasswordScreen" in src


def test_change_password_endpoint_wired() -> None:
    src = AUTH.read_text()
    assert "/auth/change-password" in src
    assert "current_password" in src and "new_password" in src


def test_authgate_branches_on_role_and_must_change() -> None:
    src = AUTH.read_text()
    assert "must_change_password" in src
    assert "restricted" in src


def test_sidebar_gates_admin_only() -> None:
    src = CHROME.read_text()
    assert "adminOnly" in src
    assert 'role === "admin"' in src


def test_app_threads_role_into_sidebar() -> None:
    src = APP.read_text()
    assert "app:auth-status" in src
    assert "userRole" in src


def test_auth_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    b = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    code = b._transform(AUTH.read_text(), "components/auth.jsx")
    assert code and "ADM_PendingAccessScreen" in code and "ADM_MustChangePasswordScreen" in code
