"""FD1b -- the per-session pause/resume/steer/cancel mutations were
extracted into a single shared hook, window.useSessionControls, instead of
being re-implemented in each caller.

The hook has no caller today. S8 gave the shell session.interrupt and
almost nothing else (S8 design section: the S1 surface the shell consumes
is "session CRUD/steer/interrupt"), because steering or resuming a session
IS sending it a message: POST .../steer invokes a CREATED session, steers
a running one, resumes a PAUSED one and reopens an ENDED one. That covers
resume and restart, so neither got a control.

It does not cover the hook's other two. Nothing you can type parks a
session, and nothing you can type ends one, so session.pause and
session.end are verbs in the shell registry, reaching the same live
POST .../pause and POST .../cancel this hook wraps.

The hook itself still has no caller and stays in the tree as working,
importable, fully-tested API rather than being deleted along with the
panels that used to call it.

Guards that the hook exists, exports the four actions, is registered in
index.html, and that the bundle transpiles with it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HOOK = UI / "components" / "use-session-controls.jsx"
INDEX = UI / "index.html"


def _index_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_hook_exists_and_is_exported() -> None:
    assert HOOK.exists(), "ui/components/use-session-controls.jsx must exist"
    src = HOOK.read_text(encoding="utf-8")
    assert "window.useSessionControls" in src


def test_hook_exposes_the_four_actions() -> None:
    src = HOOK.read_text(encoding="utf-8")
    for action in ("pause", "resume", "steer", "cancel"):
        assert action in src, f"the hook must expose {action}"


def test_the_shells_own_control_verb_is_interrupt() -> None:
    """Pinned so the hook's disuse reads as a decision, not an omission."""
    src = (UI / "components" / "shell" / "sh-doc-host.jsx").read_text(
        encoding="utf-8"
    )
    assert 'id: "session.interrupt"' in src


def test_index_registers_the_hook() -> None:
    assert "components/use-session-controls.jsx" in _index_order()
    order = _index_order()
    # After shared.jsx, which defines the primitives it consumes.
    assert order.index("components/shared.jsx") < order.index(
        "components/use-session-controls.jsx"
    )


def test_bundle_transpiles_with_hook() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/use-session-controls.jsx === */" in body.decode("utf-8")
