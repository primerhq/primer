"""FD1b — the per-session pause/resume/steer/cancel mutations were extracted
into a single shared hook, window.useSessionControls, instead of being
re-implemented in each caller (they used to be inlined in the removed
SessionDetail page AND in the Studio's ST_SessionControls cluster).

Studio's later session-panel redesign (Task 13) split ST_SessionControls into
SessionAgentPanel (End/Restart header controls; Stop lives in <Composer>) and
SessionGraphPanel (Pause/Cancel/Restart header controls) — each panel builds
its OWN control mutations again instead of calling the shared hook, since the
two panels' control sets diverged and neither needs a Resume or Steer button
(steering/resuming a session IS sending it a message). The hook itself is
unchanged and has no current caller, but stays in the tree as working,
importable, fully-tested API.

Guards that the hook exists + exports the four actions, that it loads before
studio-center in index.html, that studio-center.jsx does NOT reach for the
shared hook (each panel builds its own mutations against the signal
endpoints it actually needs), and that the bundle transpiles.
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


def test_studio_center_does_not_use_the_shared_hook() -> None:
    """SessionAgentPanel and SessionGraphPanel (Task 13) each build their own
    control mutations directly against useMutation/apiFetch/the session
    adapter instead of calling window.useSessionControls."""
    src = CENTER.read_text(encoding="utf-8")
    assert "window.useSessionControls(" not in src
    # Agent panel: End + Restart reuse the session adapter's own
    # end()/restart() (Stop is <Composer>'s own affordance, wired to the
    # adapter's stop() — no dedicated ctrl-stop testid in the header).
    assert 'data-testid="ctrl-end"' in src
    assert 'data-testid="ctrl-restart"' in src
    assert "function () { return conv.end(); }" in src
    assert "function () { return conv.restart(); }" in src
    # Graph panel: Pause is a fresh mutation (no adapter equivalent); Cancel
    # reuses conv.end() the same way the agent panel's End does.
    assert 'data-testid="ctrl-pause"' in src
    assert 'data-testid="ctrl-cancel"' in src


def test_studio_center_has_no_resume_or_steer_controls() -> None:
    """Neither panel offers a dedicated Resume or Steer control — sending a
    message through the panel's own <Composer> IS the steer/resume path
    (studio-agents-interact). Only Pause hits a fresh signal endpoint;
    Resume/Steer never existed in the redesigned panels."""
    src = CENTER.read_text(encoding="utf-8")
    assert 'encodeURIComponent(sid) + "/pause"' in src
    for frag in ('"/resume"', '"/steer"', "resumeMut", "steerMut"):
        assert frag not in src, f"unexpected resume/steer control in studio-center: {frag}"


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
