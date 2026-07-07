"""Static + transpile checks for the admin SSO-providers console page (Task 9)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SSO = ROOT / "ui" / "components" / "sso_admin.jsx"
CHROME = ROOT / "ui" / "components" / "chrome.jsx"
APP = ROOT / "ui" / "app.jsx"
ROUTER = ROOT / "ui" / "foundation" / "router.js"
INDEX = ROOT / "ui" / "index.html"


def _src() -> str:
    return SSO.read_text()


def _bundle_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_page_component_defined() -> None:
    assert "SSO_ProvidersPage" in _src()


def test_window_export() -> None:
    src = _src()
    assert "window.SSO_ProvidersPage" in src
    assert "window.SSO_Toggle" in src


def test_toggle_present() -> None:
    assert "function SSO_Toggle" in _src()


def test_crud_endpoints() -> None:
    src = _src()
    assert "/admin/oidc-providers" in src
    assert "/admin/sso-settings" in src


def test_table_testid() -> None:
    assert "sso-providers-table" in _src()


def test_table_uses_shared_tbl_class() -> None:
    """The OIDC-providers table renders with the shared console table
    styling (`.tbl` inside `.tbl-wrap`, as agents.jsx and peers do), not
    the old hand-rolled `className="table"` with per-cell inline padding."""
    src = _src()
    assert 'className="tbl-wrap"' in src
    assert 'className="tbl"' in src
    assert 'className="table"' not in src
    assert 'padding: "8px 12px"' not in src


def test_create_edit_delete_present() -> None:
    src = _src()
    assert "create-sso-provider-submit" in src
    assert "edit-sso-provider-submit" in src
    assert "delete-sso-provider-confirm-btn" in src


def test_client_secret_write_only() -> None:
    """client_secret must never be prefilled from the masked GET/list value
    in the edit dialog -- SSO_EditProviderDialog seeds its local state as
    an empty string, not from provider.client_secret."""
    src = _src()
    assert '[clientSecret, setClientSecret] = React.useState("")' in src
    # The edit dialog's local secret state is never seeded from the
    # provider's (masked) client_secret field.
    assert "useState(provider.client_secret" not in src


def test_settings_panel_present() -> None:
    src = _src()
    assert "SSO_SettingsPanel" in src
    assert "sso_jit_enabled" in src
    assert "sso_default_access" in src
    assert "<select" in src and 'className="select"' in src
    assert '"restricted"' in src and '"user"' in src


def test_registered_in_bundle_order() -> None:
    order = _bundle_order()
    assert "components/sso_admin.jsx" in order
    assert order.index("components/sso_admin.jsx") > order.index("components/shared.jsx")
    assert order.index("components/sso_admin.jsx") > order.index("components/admin_users.jsx")
    assert order.index("components/sso_admin.jsx") < order.index("app.jsx")


def test_router_has_sso_providers_route() -> None:
    src = ROUTER.read_text()
    assert "SsoProvidersPage" in src
    assert "/admin/sso-providers" in src


def test_app_wires_sso_providers_page() -> None:
    src = APP.read_text()
    assert "admin-sso-providers" in src
    assert "SSO_ProvidersPage" in src


def test_chrome_nav_has_sso_providers_entry() -> None:
    src = CHROME.read_text()
    assert "admin-sso-providers" in src
    assert "SSO Providers" in src


def test_index_has_script_tag() -> None:
    assert "components/sso_admin.jsx" in INDEX.read_text()


def test_sso_admin_transpiles() -> None:
    """Real JSX transpile via the server-side bundler (no jsdom in the py toolchain)."""
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    b = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    code = b._transform(SSO.read_text(), "components/sso_admin.jsx")
    assert code and "SSO_ProvidersPage" in code
