"""Static + transpile checks for the self-service Linked accounts console
page (Layer 2 OIDC SSO, Task 10). This is a per-user page (NOT admin) —
contrast test_sso_admin_page.py which covers the admin OIDC-providers CRUD
console."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LA = ROOT / "ui" / "components" / "linked_accounts.jsx"
CHROME = ROOT / "ui" / "components" / "chrome.jsx"
APP = ROOT / "ui" / "app.jsx"
ROUTER = ROOT / "ui" / "foundation" / "router.js"
INDEX = ROOT / "ui" / "index.html"


def _src() -> str:
    return LA.read_text()


def _bundle_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_page_component_defined() -> None:
    assert "LA_LinkedAccountsPage" in _src()


def test_window_export() -> None:
    src = _src()
    assert "window.LA_LinkedAccountsPage" in src
    assert "window.LA_UnlinkConfirmDialog" in src


def test_identities_list_fetch_anchor() -> None:
    src = _src()
    assert "/auth/sso/identities" in src
    assert "linked-accounts:list" in src
    assert "useResource" in src


def test_table_testid_and_columns() -> None:
    src = _src()
    assert "linked-accounts-table" in src
    assert "provider_name" in src
    assert "subject" in src
    assert "created_at" in src


def test_unlink_confirm_dialog_present() -> None:
    src = _src()
    assert "function LA_UnlinkConfirmDialog" in src
    assert "unlink-confirm-btn" in src


def test_unlink_deletes_identity() -> None:
    src = _src()
    assert '"DELETE"' in src
    assert "/auth/sso/identities/" in src


def test_link_provider_diff_and_redirect_anchor() -> None:
    """Providers not yet linked get a 'Link <name>' button whose onClick
    is a full-page browser navigation (not an apiFetch call) — the /v1
    prefix is required because this is a real browser URL, not an API
    base-relative path."""
    src = _src()
    assert "/auth/sso/providers" in src
    assert 'window.location.href = "/v1/auth/sso/"' in src
    assert '"/link"' in src
    # Diff against already-linked provider_ids.
    assert "linkedProviderIds" in src or "provider_id" in src
    # The provider id must be encoded before it's spliced into the nav URL
    # (guards against a future edit dropping the encode — unescaped-id nav
    # injection).
    assert "encodeURIComponent(p.id)" in src


def test_empty_state_present() -> None:
    src = _src()
    assert "linked-accounts-empty" in src
    assert "No linked accounts yet" in src


def test_registered_in_bundle_order() -> None:
    order = _bundle_order()
    assert "components/linked_accounts.jsx" in order
    assert order.index("components/linked_accounts.jsx") > order.index("components/shared.jsx")
    assert order.index("components/linked_accounts.jsx") < order.index("app.jsx")


def test_router_has_linked_accounts_route() -> None:
    src = ROUTER.read_text()
    assert "LinkedAccountsPage" in src
    assert "/settings/linked-accounts" in src


def test_app_wires_linked_accounts_page() -> None:
    src = APP.read_text()
    assert "linked-accounts" in src
    assert "LA_LinkedAccountsPage" in src


def test_chrome_nav_has_linked_accounts_entry_without_admin_only() -> None:
    """Every logged-in user (role user/admin) manages their own linked
    accounts — this nav item must NOT be adminOnly, unlike the admin SSO
    Providers entry right above it."""
    src = CHROME.read_text()
    assert '{ id: "linked-accounts", label: "Linked accounts", icon: "link" }' in src


def test_index_has_script_tag() -> None:
    assert "components/linked_accounts.jsx" in INDEX.read_text()


def test_linked_accounts_transpiles() -> None:
    """Real JSX transpile via the server-side bundler (no jsdom in the py
    toolchain)."""
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    b = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    code = b._transform(LA.read_text(), "components/linked_accounts.jsx")
    assert code and "LA_LinkedAccountsPage" in code


def test_no_duplicate_top_level_declaration_in_bundle() -> None:
    """The full-app bundle concatenates every file into one flat scope
    (see primer/api/_jsx_bundle.py); a duplicate top-level LA_* name
    anywhere would silently shadow this file's declarations (const/let are
    rewritten to `var`, so redeclarations are last-wins, not a
    SyntaxError)."""
    from primer.api._jsx_bundle import build_jsx_bundle

    ui = ROOT / "ui"
    etag, body = build_jsx_bundle(ui)
    assert etag and body
    text = body.decode("utf-8")
    assert text.count("function LA_LinkedAccountsPage(") == 1
