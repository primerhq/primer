"""Static + transpile checks for the admin Users console page (RBAC, Spec §6/§12)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMIN = ROOT / "ui" / "components" / "admin_users.jsx"
ADMIN_OVERLAY = ROOT / "ui" / "components" / "console" / "nv-system.jsx"
APP = ROOT / "ui" / "app.jsx"
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


def test_tables_use_shared_tbl_class() -> None:
    """The Users list + per-user keys tables render with the shared console
    table styling (`.tbl` inside `.tbl-wrap`, as agents.jsx and peers do),
    not the old hand-rolled `className="table"` with per-cell inline
    padding."""
    src = _src()
    assert 'className="tbl-wrap"' in src
    assert 'className="tbl"' in src
    assert 'className="table"' not in src
    assert 'padding: "8px 12px"' not in src


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


def test_the_admin_overlay_renders_the_users_page() -> None:
    """The console has no route table: the overlay host IS the wiring."""
    src = (ROOT / "ui" / "components" / "console" / "nv-system.jsx").read_text()
    assert "ADM_AdminUsersPage" in src


def test_admin_overlay_has_users_section() -> None:
    """The shell has no per-page nav: admin surfaces are sections of
    the one search-first admin overlay."""
    assert '"users"' in ADMIN_OVERLAY.read_text()


def test_keys_drilldown_present() -> None:
    """Keys drill-down: launch button, dialog, table, per-token revoke, and
    the admin token-management API path fragments (Spec: admin-api-key-management)."""
    src = _src()
    assert "keys-user-btn" in src
    assert "adm-user-keys-dialog" in src
    assert "adm-user-keys-table" in src
    assert "adm-revoke-key-btn" in src
    assert "/admin/users/" in src
    assert "/tokens" in src


def test_extract_error_reads_extensions_not_detail() -> None:
    """R5 fix: primer/api/errors.py's _http_exception_handler reduces a raw
    HTTPException({error, message}) dict detail to RFC7807's own STRING
    `detail`; the dict survives verbatim under `extensions`
    (ui/foundation/api.js's ApiError: this.envelope = envelope, this.detail
    = envelope.detail, a string). The old `envDetail && typeof envDetail
    === "object"` check on `envelope.detail` could never be true, so `code`
    was always null (message still rendered via the string fallback, so
    this was invisible in the UI)."""
    src = _src()
    start = src.index("function ADM_extractError(")
    end = src.index("\n}", start)
    body = src[start:end]
    assert "env.extensions" in body
    assert "env.detail" not in body


def test_disable_quick_action_present() -> None:
    """notes section 4: Force-rotation + Disable per row - Disable is a
    genuine one-click PATCH {disabled} action (no backend gap, unlike
    force-rotation - see the row-level comment); anti-lockout can refuse
    it too, so it needs its own inline error surface, not just the Edit
    modal's."""
    src = _src()
    assert "toggle-disabled-btn-" in src
    assert '{ disabled: !user.disabled }' in src
    start = src.index("function ADM_UserRow(")
    end = src.index("\n// ====", start)
    body = src[start:end]
    assert "ADM_extractError(err)" in body, "anti-lockout on Disable surfaces inline"


def test_generate_password_option_on_create() -> None:
    """Backend addendum (generate_password param, R5 password ruling):
    the create form offers a checkbox alternative to typing a password,
    and posts generate_password instead when checked."""
    src = _src()
    assert "adm-generate-password" in src
    assert "generatePassword" in src
    assert "body.generate_password = true" in src


def test_force_rotation_quick_action_present() -> None:
    """notes section 4: Force-rotation per row is now a real one-click
    action (the backend gap that used to block it is closed) - PATCHes
    generate_password: true and shows the returned plaintext once via
    the shared ADM_PasswordOneTimeDialog."""
    src = _src()
    assert "force-rotation-btn-" in src
    assert "generate_password: true" in src
    assert "ADM_PasswordOneTimeDialog" in src
    assert "window.ADM_PasswordOneTimeDialog" in src


def test_plaintext_one_time_display_present() -> None:
    src = _src()
    assert "plaintext-display" in src
    assert "copy-password-btn" in src


def test_admin_users_transpiles() -> None:
    """Real JSX transpile via the server-side bundler (no jsdom in the py toolchain)."""
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    b = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    code = b._transform(ADMIN.read_text(), "components/admin_users.jsx")
    assert code and "ADM_AdminUsersPage" in code
