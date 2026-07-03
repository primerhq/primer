"""FD1b — the per-session pause/resume/steer/cancel mutations are a single
shared hook, window.useSessionControls, instead of being re-implemented in
each caller (they used to be inlined in the removed SessionDetail page AND in
the Studio's ST_SessionControls cluster).

Guards that the hook exists + exports the four actions, that studio-center.jsx
consumes it (and no longer builds the control mutations inline), that it loads
before studio-center in index.html, and that the bundle transpiles.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HOOK = UI / "components" / "use-session-controls.jsx"
CENTER = UI / "components" / "studio-center.jsx"
INDEX = UI / "index.html"


def _index_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_hook_file_exists_and_exports() -> None:
    assert HOOK.exists(), "use-session-controls.jsx must exist"
    src = HOOK.read_text(encoding="utf-8")
    assert "function useSessionControls(" in src
    assert "window.useSessionControls = useSessionControls" in src


def test_hook_returns_the_four_actions() -> None:
    src = HOOK.read_text(encoding="utf-8")
    assert "return { pause: pause, resume: resume, steer: steer, cancel: cancel }" in src


def test_hook_hits_the_workspace_scoped_endpoints() -> None:
    src = HOOK.read_text(encoding="utf-8")
    # Same workspace-scoped signal endpoints the inline versions used.
    assert '"/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/" + action' in src
    for action in ('signal("pause")', 'signal("resume")', 'signal("cancel")'):
        assert action in src, f"missing {action}"
    assert '"/steer"' in src, "steer posts to the /steer endpoint with an instruction body"


def test_studio_center_uses_the_shared_hook() -> None:
    src = CENTER.read_text(encoding="utf-8")
    assert "window.useSessionControls(wid, sid" in src, "ST_SessionControls must call the shared hook"
    # It threads the caller-specific bits through the hook options.
    assert "onSteerSuccess" in src
    assert '"studio-session:" + sid' in src


def test_studio_center_no_longer_inlines_control_mutations() -> None:
    src = CENTER.read_text(encoding="utf-8")
    # The four control mutations must be gone from studio-center (they live in
    # the hook now). No POST to a session signal endpoint should remain here.
    for frag in (
        'encodeURIComponent(sid) + "/pause"',
        'encodeURIComponent(sid) + "/resume"',
        'encodeURIComponent(sid) + "/cancel"',
        'encodeURIComponent(sid) + "/steer"',
    ):
        assert frag not in src, f"control endpoint still inlined in studio-center: {frag}"


def test_index_loads_hook_before_studio_center() -> None:
    order = _index_order()
    assert "components/use-session-controls.jsx" in order, "hook not registered in index.html"
    assert order.index("components/use-session-controls.jsx") < order.index("components/studio-center.jsx")


def test_bundle_transpiles_with_hook() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/use-session-controls.jsx === */" in text
