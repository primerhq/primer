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


def test_the_gate_chain_is_auth_then_shell() -> None:
    """S5 boot probe drives login -> password -> wizard -> shell (C5).

    ONE gate owns that chain. AuthGate returns the wizard when
    ``setup_complete`` is false and its children otherwise (S5 P2 Task
    9), so the shell must not branch on setup a second time: two gates
    for one decision is how a console ends up rendering the wizard
    forever [CROSSPLAN 2026-08-16, F27].
    """
    src = _src()
    assert "SetupWizardGate" not in src
    assert "setup_complete" not in src
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


def test_the_gate_that_picks_the_workspace_follows_the_url() -> None:
    """SH_RootGate must re-read the URL when the URL changes.

    It chooses the workspace, and it read the hash once at first render
    only. Nothing that changes the hash could then move the shell off
    whichever workspace it happened to boot on: not a deep link to
    another workspace, not Back, and not the shell's own Switch
    Workspace verb, which works by assigning window.location.hash.
    SH_Shell listened for the same events the whole time, but only ever
    updated the overlay, the doc and the anchor from them.

    It cost a whole round of ui_e2e debugging: three journeys mocked
    their own workspace's pending-yields route and the shell sat there
    polling the first workspace in the list instead, so the mock matched
    nothing and the surface rendered empty.
    """
    src = _src()
    gate = src.split("function SH_RootGate()")[1]
    assert "hashchange" in gate, (
        "SH_RootGate never re-reads the URL, so the workspace it picks at "
        "boot is the only one it will ever show"
    )
    assert "popstate" in gate, "Back must move the shell too"


def test_no_router_dependency() -> None:
    """The palette is the router: the classic route table dies in P5."""
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
