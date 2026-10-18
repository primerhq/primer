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


# The Platform sidebar's rows carry their verb as a COMPUTED data-verb: one expression for every nav (data-verb={"platform.open." + id}) and one for every provider type
# (data-verb={"platform.open.providers." + t}), so the literal-attribute scan cannot read them (Platform revamp 01, Task 9). They count as rendered exactly while BOTH expressions
# are in nv-platform.jsx, and the verbs are derived from the grammar the rows are drawn from: platform.open.<nav> for every nav of SH_VIEWS.platform and
# platform.open.providers.<type> for every type of SH_VIEW_SUBS. A custom page's plan registers its own literal verb (platform.open.web, platform.open.templates) and relies on this.
NAV_ROW_ATTRIBUTES = ('data-verb={"platform.open." + id}', 'data-verb={"platform.open.providers." + t}')


def _nav_row_verbs(platform_source: str) -> set[str]:
    if not all(attribute in platform_source for attribute in NAV_ROW_ATTRIBUTES):
        return set()
    navs = _mini_racer_value(FOUNDATION / "shell-url.js", "SH_VIEWS.platform")
    types = _mini_racer_value(FOUNDATION / "shell-url.js", 'SH_VIEW_SUBS["platform:providers"]')
    return {f"platform.open.{nav}" for nav in navs} | {f"platform.open.providers.{kind}" for kind in types}


# Chrome-owned verbs: the control is the affordance; the testid
# pins below keep them honest.
CHROME_OWNED = {
    "palette.open": 'data-testid="nv-search"',
    "terminal.toggle": 'data-testid="nv-toggle-terminal"',
    # workspace.switch/create moved OFF chrome onto the rail's own
    # data-verb attributes (US-012b), so the primary rendered-check
    # covers them and they need no chrome witness here anymore.
    "view.studio": 'data-testid="nv-go-studio"',
    "view.platform": 'data-testid="nv-go-platform"',
    "view.system": "nv-menu-row",
}


def _verbs_without_a_pointer_affordance(registrations: list[dict[str, str]], text: str, platform_source: str) -> list[str]:
    """The ids of the registrations that declare a pointer surface and render none: no data-verb of their own, no sidebar row (the nav-row verbs), no chrome control."""
    rendered = set(re.findall(r'data-verb="([\w.]+)"', text)) | _nav_row_verbs(platform_source)
    missing = []
    for entry in registrations:
        vid = entry.get("id")
        if not vid:
            continue
        names = set(re.findall(r'"([\w-]+)"', entry["surfaces"]))
        if not (names - {"palette"}):
            continue
        if vid in rendered:
            continue
        witness = CHROME_OWNED.get(vid)
        if witness and witness in text:
            continue
        missing.append(vid)
    return missing


def test_every_registered_verb_has_a_pointer_affordance() -> None:
    """A declared pointer surface must actually render: every verb with
    a non-palette surface carries a data-verb affordance somewhere, or
    is one of the chrome verbs whose affordance IS the chrome control
    (topbar toggles, the search field, the workspace selector)."""
    missing = _verbs_without_a_pointer_affordance(_registrations(), _all_console_text(), _console_sources()["nv-platform.jsx"])
    assert not missing, f"verbs with no pointer affordance: {sorted(missing)}"


def test_platform_nav_is_a_pointer_surface() -> None:
    """The Platform sidebar is a surface of its own: the verbs for its rows declare it, and it counts as a pointer (the registry refuses a verb whose only surface is the palette)."""
    shell_verbs = FOUNDATION / "shell-verbs.js"
    assert _mini_racer_value(shell_verbs, "SH_SURFACES").count("platform-nav") == 1
    register = (
        "(function (surfaces) { try { SH_createVerbRegistry().register({ id: 'x.y', label: 'Open Thing', surfaces: surfaces, run: function () {} }); return 'ok'; }"
        " catch (e) { return String(e.message || e); } })"
    )
    assert _mini_racer_value(shell_verbs, register + "(['platform-nav'])") == "ok", "a verb whose pointer surface is the sidebar registers on its own"
    assert "declares no pointer surface" in _mini_racer_value(shell_verbs, register + "(['palette'])"), "while the palette alone is still refused"


def test_the_nav_row_verbs_are_every_nav_and_every_provider_type() -> None:
    platform = _console_sources()["nv-platform.jsx"]
    verbs = _nav_row_verbs(platform)
    navs = _mini_racer_value(FOUNDATION / "shell-url.js", "SH_VIEWS.platform")
    types = _mini_racer_value(FOUNDATION / "shell-url.js", 'SH_VIEW_SUBS["platform:providers"]')
    assert verbs == {f"platform.open.{n}" for n in navs} | {f"platform.open.providers.{t}" for t in types}
    assert {"platform.open.web", "platform.open.templates", "platform.open.providers.tts"} <= verbs, "custom pages and the last types are covered"


def test_the_nav_row_verbs_count_only_while_both_row_attributes_are_in_the_source() -> None:
    """The extension is gated on the literal attributes: remove either one from nv-platform.jsx and a verb that only the sidebar row renders is palette-only again."""
    platform = _console_sources()["nv-platform.jsx"]
    assert _nav_row_verbs(platform), "the real source carries both"
    for attribute in NAV_ROW_ATTRIBUTES:
        assert attribute in platform, attribute
        assert _nav_row_verbs(platform.replace(attribute, "")) == set(), f"without {attribute} the nav rows render no verb"


def test_a_custom_pages_literal_verb_is_rendered_by_its_row_and_an_unknown_one_is_not() -> None:
    """What Plans 04 and 08 rely on: ``reg({ id: "platform.open.web", ..., surfaces: ["palette", "platform-nav"] })`` needs no affordance of its own."""
    platform = _console_sources()["nv-platform.jsx"]
    web = {"id": "platform.open.web", "surfaces": '"palette", "platform-nav"'}
    templates = {"id": "platform.open.templates", "surfaces": '"palette", "platform-nav"'}
    typo = {"id": "platform.open.webb", "surfaces": '"palette", "platform-nav"'}
    palette_only_type = {"id": "platform.open.providers.nope", "surfaces": '"palette", "platform-nav"'}
    assert _verbs_without_a_pointer_affordance([web, templates], _all_console_text(), platform) == []
    assert _verbs_without_a_pointer_affordance([web, typo, palette_only_type], _all_console_text(), platform) == ["platform.open.webb", "platform.open.providers.nope"]
    assert _verbs_without_a_pointer_affordance([web], _all_console_text(), platform.replace(NAV_ROW_ATTRIBUTES[0], "")) == ["platform.open.web"], (
        "with the row attribute gone, the custom page's verb has no pointer affordance"
    )


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


# Known browser-reserved combos: the OS/browser eats the keydown before the
# page's own listener ever runs (tab/window management), or one major
# browser dedicates it to something else (Firefox's private-window
# shortcut). A verb bound to one of these is silently dead no matter how
# correct the dispatcher is (2026-08-29 UI review, F6 - session.create was
# Ctrl+N, workspace.switch was Ctrl+Shift+P). Not exhaustive - extend as
# new dead chords surface.
RESERVED_CHORDS = {
    "ctrl+n", "ctrl+t", "ctrl+w",
    "ctrl+shift+n", "ctrl+shift+t", "ctrl+shift+w",
    "ctrl+shift+p", "ctrl+shift+q",
}


def test_no_registered_chord_is_browser_reserved() -> None:
    offenders = []
    for entry in _registrations():
        chord = entry.get("chord")
        if chord and chord.lower() in RESERVED_CHORDS:
            offenders.append(f'{entry.get("id")}: {chord}')
    assert not offenders, f"browser-reserved chords: {offenders}"
