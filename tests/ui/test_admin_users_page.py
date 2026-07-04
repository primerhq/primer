"""Static + transpile checks for the admin Users console page (RBAC, Spec §6/§12)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMIN = ROOT / "ui" / "components" / "admin_users.jsx"
CHROME = ROOT / "ui" / "components" / "chrome.jsx"
APP = ROOT / "ui" / "app.jsx"
ROUTER = ROOT / "ui" / "foundation" / "router.js"
INDEX = ROOT / "ui" / "index.html"


def _src() -> str:
    return ADMIN.read_text()


def _bundle_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_page_component_defined() -> None:
    assert "ADM_AdminUsersPage" in _src()


def test_window_export() -> None:
    assert "window.ADM_AdminUsersPage" in _src()


def test_crud_endpoint() -> None:
    assert "/admin/users" in _src()


def test_table_testid() -> None:
    assert "admin-users-table" in _src()


def test_create_and_delete_present() -> None:
    src = _src()
    assert "create-user-submit" in src
    assert "delete-user-confirm-btn" in src


def test_role_options_present() -> None:
    src = _src()
    assert "restricted" in src and '"admin"' in src and '"user"' in src


def test_registered_in_bundle_order() -> None:
    order = _bundle_order()
    assert "components/admin_users.jsx" in order
    assert order.index("components/admin_users.jsx") > order.index("components/shared.jsx")
    assert order.index("components/admin_users.jsx") < order.index("app.jsx")


def test_router_has_admin_users_route() -> None:
    assert "AdminUsersPage" in ROUTER.read_text()
    assert "/admin/users" in ROUTER.read_text()


def test_app_wires_admin_users_page() -> None:
    src = APP.read_text()
    assert "admin-users" in src
    assert "ADM_AdminUsersPage" in src


def test_chrome_nav_has_users_entry() -> None:
    assert "admin-users" in CHROME.read_text()


def test_admin_users_transpiles() -> None:
    """Real JSX transpile via the server-side bundler (no jsdom in the py toolchain)."""
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    b = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    code = b._transform(ADMIN.read_text(), "components/admin_users.jsx")
    assert code and "ADM_AdminUsersPage" in code
