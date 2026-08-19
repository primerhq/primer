"""The shell root: gate chain, landing rule, URL sync, region contract."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-shell.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_and_exported() -> None:
    assert 'src="components/shell/sh-shell.jsx"' in (UI / "index.html").read_text(
        encoding="utf-8"
    )
    src = _src()
    assert "window.SH_Shell = SH_Shell;" in src
    assert "window.SH_RootGate = SH_RootGate;" in src
    assert "window.SH_useShell = SH_useShell;" in src


def test_the_gate_chain_is_auth_then_setup_then_shell() -> None:
    """S5 boot probe drives login -> password -> wizard -> shell (C5).

    TRANSITIONAL while both shells coexist (P2 to P4). SH_RootGate keeps
    its own setup_complete branch only because ui/app.jsx still routes
    through S2_RootGate; S5 P2 Task 9 owns the same branch inside AuthGate.
    P5 Task 28 deletes this branch and rewrites this test to assert the
    inverse [CROSSPLAN 2026-08-16, F27].
    """
    src = _src()
    assert "SetupWizardGate" in src
    assert "setup_complete" in src
    app = (UI / "app.jsx").read_text(encoding="utf-8")
    assert "SH_RootGate" in app
    assert re.search(r"AuthGate[\s\S]{0,400}SH_RootGate", app)


def test_the_shell_renders_the_four_regions() -> None:
    src = _src()
    for testid in ("shell-root", "shell-topbar", "shell-rail", "shell-center",
                   "shell-statusbar"):
        assert f'data-testid="{testid}"' in src, testid


def test_landing_opens_the_most_recent_session_and_lazily_creates_one() -> None:
    """Spec section 3: default workspace -> most recent session, lazy create."""
    src = _src()
    assert "SH_api.sessions" in src
    assert "SH_api.createSession" in src
    assert "last_activity_at" in src


def test_url_is_the_state_and_verb_navigation_pushes_history() -> None:
    src = _src()
    assert "SH_parseUrl" in src and "SH_buildUrl" in src
    assert "pushState" in src
    assert "hashchange" in src
    # Transient UI must never be serialised.
    assert "paletteOpen: " not in src.split("SH_buildUrl(")[1][:400]


def test_no_router_dependency() -> None:
    """The palette is the router; foundation/router.js dies in P5."""
    src = _src()
    for banned in ("useRouter", "ROUTES", "navigate("):
        assert banned not in src, banned


def test_shell_has_no_raw_color_literals() -> None:
    src = _src()
    depathed = src.replace("#/w/", "").replace("#/", "")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", depathed), (
        "raw hex color literal; every color rides a var(--*) token"
    )


def test_bundle_transpiles_every_shell_file() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    for f in sorted((UI / "components" / "shell").glob("*.jsx")):
        assert f"/* === components/shell/{f.name} === */" in text, (
            f"{f.name} missing from the bundle; is its index.html script tag there?"
        )
