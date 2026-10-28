"""G13 fix B (the mobile batch): G13-03 the workspace settings overlay's absence from the phone shell is documented as a deliberate exclusion (docs path, lead ruling); G13-04 the terminal verb is hidden from the mobile band's registry (palette AND chord, one mechanism: the verb is not registered for the band); G13-05 the two platform-section load errors announce to assistive tech (role="alert", parity with the phone Inbox's error in the same console).

Audit: /var/tmp/revamp/audit/G13.md; the auditor's probes pin the RED state, these pin the GREEN state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.ui._platform_page_harness import platform_page_context

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


SHELL = read("ui/components/console/nv-shell.jsx")
PLATFORM = read("ui/components/console/nv-platform.jsx")
MOBILE = read("ui/components/console/nv-mobile-shell.jsx")
UI_PAGES = read("docs/dev/subsystems/ui-pages.md")
SHELL_URL = read("ui/foundation/shell-url.js")
SHELL_VERBS = read("ui/foundation/shell-verbs.js")


def _effect_source() -> str:
    """The shell's core-verb effect body, exactly as the house harnesses cut it out."""
    start = SHELL.index("// --- Core verbs (chrome-level; view bodies add their own)")
    body = SHELL.index("React.useEffect(function () {", start) + len("React.useEffect(function () {")
    return SHELL[body:SHELL.index("}, [registry]);", body)]


def _registry_with(is_mobile: bool):
    """The real registry after the committed core-verb effect has run for one band: the
    registration logic as nv-shell.jsx writes it, the real SH_createVerbRegistry."""
    ctx = platform_page_context(prelude_extra=SHELL_URL + SHELL_VERBS)
    ctx.eval(
        "var registry = SH_createVerbRegistry(); var WARNS = [];"
        "console.warn = function (m) { WARNS.push(String(m)); };"
        "(function (registry, goView, isMobile) {" + _effect_source() + "\n}"
        ")(registry, function () {}, " + ("true" if is_mobile else "false") + ");"
    )
    return ctx


# ---- G13-04: the terminal verb is a desktop-band registration -------------------------------------------------


def test_g13_04_the_terminal_verb_is_absent_from_the_mobile_band_registry():
    """The verb is registered for the desktop band only (nv-shell.jsx): on a phone it
    would flip con.panels.terminal, which only the desktop studio frame renders, so the
    palette would advertise a no-op and the Ctrl+j chord would bind to one. Absent from
    the registry means absent from the palette list AND the chord dispatcher (both walk
    the registry); the other core verbs are untouched."""
    ctx = _registry_with(True)
    try:
        ids = json.loads(ctx.eval("JSON.stringify(registry.ids())"))
        assert "terminal.toggle" not in ids, "the terminal verb is registered for the mobile band"
        for verb in ("view.studio", "view.platform", "view.system", "session.create", "palette.open"):
            assert verb in ids, "an unrelated core verb is missing: " + verb
    finally:
        ctx.close()


def test_g13_04_the_terminal_verb_keeps_its_chord_on_the_desktop_band():
    """Desktop is unchanged: the verb registers with its Ctrl+j chord (the studio frame
    still mounts NV_Terminal under the panel it flips)."""
    ctx = _registry_with(False)
    try:
        rec = json.loads(ctx.eval(
            "JSON.stringify((function () { var v = registry.get('terminal.toggle');"
            " return v ? { id: v.id, chord: v.chord } : null; })())"
        ))
        assert rec == {"id": "terminal.toggle", "chord": "Ctrl+j"}, rec
    finally:
        ctx.close()


# ---- G13-03 (docs path): the settings surfaces are documented desktop-only ------------------------------------


def test_g13_03_the_settings_surfaces_are_documented_next_to_the_delete_exclusion():
    """The MEDIUM is the UNDOCUMENTED absence: every surface of the workspace settings
    overlay (rename, terminal access, Logs, Events, Channels, Destroy) must be named as
    desktop-only in a note that sits right next to the pre-existing workspace-delete
    exclusion in ui-pages.md, with the reason (the phase plan built all 14 platform
    sections minus the documented Workspaces absence; the M5 intercept cannot route the
    overlay because it is not in NV_PLAT_PAGES; the hand-pasted link falls through)."""
    anchor = UI_PAGES.index("the phone shell has no workspace delete")
    nxt = UI_PAGES.index("\n- **", anchor)
    note = UI_PAGES[nxt:UI_PAGES.index("\n- **", nxt + 1)]
    low = note.lower()
    for word in ("rename", "terminal access", "logs", "events", "channels", "destroy"):
        assert word in low, "the exclusion note does not name the " + word + " surface"
    assert "desktop-only" in low, "the note must state the surfaces are desktop-only"
    assert "nv_plat_pages" in low, "the note must name the NV_PLAT_PAGES routing gap (the M5 intercept)"


# ---- G13-05: the section load errors announce -----------------------------------------------------------------


def test_g13_05_both_platform_section_error_divs_carry_role_alert():
    """The refusal is terminal for that read (the stale-while-error state does not
    re-announce on poll recovery), so both section error divs take role="alert", the
    parity the phone Inbox's failed load in the same console already has. The loading
    states keep role="status" (the probe asserts they did not change)."""
    m = re.search(r"<div[^>]*data-testid=\"nv-plat-error\"[^>]*>", PLATFORM)
    assert m, "the desktop nv-plat-error div is gone"
    assert 'role="alert"' in m.group(0), "the desktop section error does not announce: %r" % m.group(0)
    m2 = re.search(r"<div[^>]*nv-mob-plat-error[^>]*>", MOBILE)
    assert m2, "the phone nv-mob-plat-error div is gone"
    assert 'role="alert"' in m2.group(0), "the phone section error does not announce: %r" % m2.group(0)
    # Parity and non-regression guards: the states that already announce stay as they are.
    m3 = re.search(r"<div[^>]*data-testid=\"nv-plat-loading\"[^>]*>", PLATFORM)
    assert m3 and 'role="status"' in m3.group(0), "the desktop loading line lost role=status"
    m4 = re.search(r"<div[^>]*data-testid=\"nv-mob-ib-error\"[^>]*>", MOBILE)
    assert m4 and 'role="alert"' in m4.group(0), "the phone inbox error lost role=alert"
    m5 = re.search(r"<div[^>]*nv-mob-plat-loading[^>]*>", MOBILE)
    assert m5 and 'role="status"' in m5.group(0), "the phone loading line lost role=status"
