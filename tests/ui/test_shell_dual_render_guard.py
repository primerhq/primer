"""The DUAL-RENDER guard, re-aimed at the console on the flag day.

The palette is the router, so a surface with no verb is unreachable and
a verb with no pointer affordance is palette-only. Both stay prohibited;
what changed is the mechanism: the console registers verbs through the
reg() wrapper in nv-shell and renders pointer affordances as
data-verb attributes, overlays are opened by con.openOverlay (or the
System view renders the same surface as a nav), and doc kinds are
opened by con.setDoc / the adapter's openDoc.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CONSOLE = UI / "components" / "console"
FOUNDATION = UI / "foundation"


def _console_sources() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(CONSOLE.glob("*.jsx"))
    }


def _all_console_text() -> str:
    return "\n".join(_console_sources().values())


def _registrations() -> list[dict[str, str]]:
    """Every reg({...}) / registry.register({...}) literal, as a dict of
    the scalar fields the guard cares about."""
    out: list[dict[str, str]] = []
    for body in _console_sources().values():
        for match in re.finditer(r"(?:\breg|registry\.register)\(\{", body):
            start = match.end() - 1
            depth = 0
            for i in range(start, len(body)):
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                    if depth == 0:
                        block = body[start:i + 1]
                        break
            else:  # pragma: no cover - unbalanced source is a syntax error
                pytest.fail("unbalanced register() literal")
            entry: dict[str, str] = {}
            vid = re.search(r'id:\s*"([\w.]+)"', block)
            if vid:
                entry["id"] = vid.group(1)
            surfaces = re.search(r"surfaces:\s*\[([^\]]*)\]", block)
            entry["surfaces"] = surfaces.group(1) if surfaces else ""
            chord = re.search(r'chord:\s*"([^"]+)"', block)
            if chord:
                entry["chord"] = chord.group(1)
            out.append(entry)
    return out


def _mini_racer_value(module: Path, expr: str):
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(module.read_text(encoding="utf-8"))
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def test_every_registered_verb_declares_a_pointer_surface() -> None:
    """Nothing is palette-only (the dual-render rule)."""
    known = set(_mini_racer_value(FOUNDATION / "shell-verbs.js", "SH_SURFACES"))
    offenders = []
    for entry in _registrations():
        names = set(re.findall(r'"([\w-]+)"', entry["surfaces"]))
        assert names <= known, f'{entry.get("id")}: unknown surface in {names}'
        if not (names - {"palette"}):
            offenders.append(entry.get("id"))
    assert not offenders, f"palette-only verbs: {offenders}"


def test_every_registered_verb_has_a_pointer_affordance() -> None:
    """A declared pointer surface must actually render: every verb with
    a non-palette surface carries a data-verb affordance somewhere, or
    is one of the chrome verbs whose affordance IS the chrome control
    (topbar toggles, the search field, the workspace selector)."""
    text = _all_console_text()
    rendered = set(re.findall(r'data-verb="([\w.]+)"', text))
    # Chrome-owned verbs: the control is the affordance; the testid
    # pins below keep them honest.
    chrome_owned = {
        "palette.open": 'data-testid="nv-search"',
        "terminal.toggle": 'data-testid="nv-toggle-terminal"',
        "events.toggle": 'data-testid="nv-toggle-events"',
        "workspace.switch": 'data-testid="nv-ws-btn"',
        "workspace.create": 'data-testid="nv-ws-create"',
        "view.studio": 'data-testid="nv-go-studio"',
        "view.platform": 'data-testid="nv-go-platform"',
        "view.system": "nv-menu-row",
    }
    missing = []
    for entry in _registrations():
        vid = entry.get("id")
        if not vid:
            continue
        names = set(re.findall(r'"([\w-]+)"', entry["surfaces"]))
        if not (names - {"palette"}):
            continue
        if vid in rendered:
            continue
        witness = chrome_owned.get(vid)
        if witness and witness in text:
            continue
        missing.append(vid)
    assert not missing, f"verbs with no pointer affordance: {sorted(missing)}"


def test_every_overlay_is_reachable_from_the_registry() -> None:
    """An overlay name no affordance opens is an address only a pasted
    link can reach. Reachability witnesses: an openOverlay("<name>")
    call, the shell's own setOverlay literal (the create verbs), or the
    System view rendering the same surface as a nav row."""
    overlays = _mini_racer_value(FOUNDATION / "shell-url.js", "SH_OVERLAYS")
    text = _all_console_text()
    system_equivalents = {
        "activity": 'data-testid={"nv-sys-row:" + id}',
        "internal-collections": 'data-testid={"nv-sys-row:" + id}',
    }
    unreachable = []
    for name in overlays:
        if f'openOverlay("{name}"' in text:
            continue
        if f'{{ name: "{name}", section: null, id: null }}' in text:
            continue
        witness = system_equivalents.get(name)
        if witness and witness in text:
            continue
        unreachable.append(name)
    assert not unreachable, f"unreachable overlays: {sorted(unreachable)}"


def test_every_doc_kind_is_reachable_from_the_registry() -> None:
    """An addressable doc kind no affordance opens is an orphan the URL
    can reach but the user cannot."""
    kinds = _mini_racer_value(FOUNDATION / "shell-url.js", "SH_DOC_KINDS")
    text = _all_console_text()
    openers = set(re.findall(r'(?:setDoc|openDoc)\(\{\s*kind:\s*"(\w+)"', text))
    missing = set(kinds) - openers
    assert not missing, f"doc kinds nothing opens: {sorted(missing)}"


def test_every_chorded_verb_id_exists() -> None:
    """A chord is a live binding through the registry dispatcher, so a
    chord on an unregistered verb cannot happen by construction - what
    CAN break is the dispatcher itself. Pin it."""
    shell = _console_sources()["nv-shell.jsx"]
    assert "chordMatches" in shell and "registry.all()" in shell
    chorded = [e for e in _registrations() if e.get("chord")]
    assert chorded, "at least the core chords must be registered"
    for entry in chorded:
        assert entry.get("id"), f"chord {entry['chord']} on an id-less verb"


def test_every_data_verb_attribute_names_a_registered_verb() -> None:
    """The pointer affordances render data-verb="<id>"; a typo there is a
    button that looks live and does nothing."""
    referenced = set(re.findall(r'data-verb="([\w.]+)"', _all_console_text()))
    registered = {e["id"] for e in _registrations() if "id" in e}
    missing = referenced - registered
    assert not missing, f"data-verb pointing at nothing: {sorted(missing)}"
