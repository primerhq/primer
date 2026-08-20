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
    # Documents and tabs are workspace-scoped, so a switch must drop
    # them: carrying them makes the shell ask the new workspace for the
    # old one's sessions, which 404 on every poll.
    shell_body = src.split("function SH_Shell(")[1]
    assert "lastWidRef" in shell_body, (
        "a workspace switch must reset the shell's documents rather than "
        "carrying the previous workspace's tabs into it"
    )


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


# ---------------------------------------------------------------------------
# Landing must not act for a workspace the shell has already left.
# ---------------------------------------------------------------------------


def test_the_lazy_create_is_dropped_if_the_workspace_changed() -> None:
    """Regression: a deep-linked session tab never appeared.

    Landing on an empty workspace starts a createSession round trip. The
    shell does not stand still while it runs: following a url to a
    document in another workspace resolved the promise after the move and
    pinned the abandoned workspace's session over the tab the url had
    just opened. The rail showed the workspace you asked for and the
    center showed a session that does not belong to it, so the tab named
    in the url was never there.
    """
    src = _src()
    create = src[src.index("SH_api.createSession(wid, {})"):]
    create = create[:create.index("});")]
    assert "widRef.current !== startedForWid" in create, (
        "the create must be dropped when the shell has left the workspace "
        "it was started for"
    )
    assert "SH_readUrl().doc" in create, (
        "and dropped when the url has since named a document of its own"
    )
    assert "var startedForWid = wid;" in src


def test_landing_ignores_session_rows_from_another_workspace() -> None:
    """The sessions snapshot outlives the render that changes workspace."""
    src = _src()
    assert re.search(
        r"items\[0\]\.workspace_id\s*\n?\s*&&\s*items\[0\]\.workspace_id\s*!==\s*wid",
        src,
    ), "landing must wait for the refetch rather than open a foreign session"


def test_the_url_document_is_open_on_the_first_render() -> None:
    """Regression: a shell mounted at a document url opened nothing.

    The overlay and the anchor were seeded from the url in the initial
    state; the document was not, and nothing else opens it on a first
    render. The hashchange listener needs a hash change AFTER mount and
    the workspace effect needs the workspace to change AFTER mount, so a
    shell that mounts already pointed at "#/w/<wid>?doc=session:<id>"
    ignored the session the address named. The landing rule then decided
    the workspace was empty and created a session of its own, which is
    the tab that appeared instead of the one asked for.
    """
    src = _src()
    seed = src[src.index("var docsState = React.useState("):]
    seed = seed[:seed.index("var overlayState")]
    assert "initial.doc" in seed, (
        "the initial doc state must be seeded from the url, like the "
        "overlay and the anchor beside it"
    )
    assert "SH_openDoc(" in seed
