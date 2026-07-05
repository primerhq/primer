"""Static + transpile checks for the Layer 2 OIDC 'Sign in with X' buttons
on the login screen (`_SsoButtons` in ui/components/auth.jsx)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
AUTH = UI / "components" / "auth.jsx"


def _src() -> str:
    return AUTH.read_text(encoding="utf-8")


def test_sso_buttons_component_defined() -> None:
    assert "_SsoButtons" in _src()


def test_fetches_sso_providers() -> None:
    src = _src()
    assert '"/auth/sso/providers"' in src
    assert "window.primerApi.apiFetch" in src


def test_redirect_anchor_present() -> None:
    """Clicking a provider button must be a full-page navigation (not an
    apiFetch call) to the backend's OIDC redirect endpoint, so the URL
    carries the /v1 prefix (a real browser URL, not an API base path)."""
    assert 'window.location.href = "/v1/auth/sso/"' in _src()


def test_rendered_inside_login_screen_after_form() -> None:
    src = _src()
    login_start = src.index("function LoginScreen(")
    form_close = src.index("</form>", login_start)
    sso_render = src.index("<_SsoButtons", login_start)
    assert sso_render > form_close, "_SsoButtons must render after the login form"


def test_auth_transpiles_via_server_bundler() -> None:
    """Real JSX transpile via the server-side bundler (no jsdom in the py toolchain)."""
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_src(), "components/auth.jsx")
    assert code and "_SsoButtons" in code


def test_no_duplicate_top_level_declaration_in_bundle() -> None:
    """The full-app bundle concatenates every file into one flat scope
    (see primer/api/_jsx_bundle.py); a duplicate top-level `_SsoButtons`
    declaration anywhere would silently shadow this one (const/let are
    rewritten to `var`, so redeclarations are last-wins, not a SyntaxError)."""
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert text.count("function _SsoButtons(") == 1
