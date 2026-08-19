"""The DUAL-RENDER guard: section 6's successor to the router guard.

tests/ui/test_sidebar_routes_resolve.py dies with the router it guards.
This replaces it, and it guards a bigger thing: the palette is the
router, so a surface with no verb is unreachable, and a verb with no
pointer affordance is palette-only. Both are prohibited.

Three assertions, all static, exactly as section 6 words them:
  1. every registered verb renders a pointer affordance FROM the registry
  2. every registered overlay and doc kind is reachable from the registry
  3. every verb id referenced by chords or menus exists
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHELL = UI / "components" / "shell"
FOUNDATION = UI / "foundation"


def _shell_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(SHELL.glob("*.jsx"))}


def _all_shell_text() -> str:
    return "\n".join(_shell_sources().values())


def _registrations() -> list[dict[str, str]]:
    """Every registry.register({...}) literal in the shell, as a dict of
    the scalar fields the guard cares about."""
    out: list[dict[str, str]] = []
    for body in _shell_sources().values():
        for match in re.finditer(r"registry\.register\(\{", body):
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
            out.append(entry)
    return out


def _mini_racer_value(module: Path, expr: str):
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(module.read_text(encoding="utf-8"))
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def test_every_registered_verb_declares_a_pointer_surface() -> None:
    """Nothing is palette-only (section 8's dual-render rule)."""
    known = set(_mini_racer_value(FOUNDATION / "shell-verbs.js", "SH_SURFACES"))
    offenders = []
    for entry in _registrations():
        names = set(re.findall(r'"([\w-]+)"', entry["surfaces"]))
        assert names <= known, f'{entry.get("id")}: unknown surface in {names}'
        if not (names - {"palette"}):
            offenders.append(entry.get("id"))
    assert not offenders, f"palette-only verbs: {offenders}"


def test_every_declared_surface_actually_renders_from_the_registry() -> None:
    """A surface nobody calls forSurface() for is a declaration that
    renders nothing, which is the failure mode the rule exists to
    prevent."""
    text = _all_shell_text()
    declared: set[str] = set()
    for entry in _registrations():
        declared |= set(re.findall(r'"([\w-]+)"', entry["surfaces"]))
    declared.discard("palette")  # the palette renders every verb by design
    rendered = set(re.findall(r'forSurface\("([\w-]+)"\)', text))
    missing = declared - rendered
    assert not missing, f"surfaces declared but never rendered: {sorted(missing)}"


def test_every_overlay_is_reachable_from_the_registry() -> None:
    overlays = _mini_racer_value(FOUNDATION / "shell-url.js", "SH_OVERLAYS")
    text = _all_shell_text()
    # Task 15 generates one overlay.open.<name> verb per entry; assert the
    # generator plus the label map that feeds it, then each name.
    assert '"overlay.open." + name' in text
    labels = re.search(r"var SH_OVERLAY_LABELS = \{([\s\S]*?)\n\};", text)
    assert labels, "the overlay label map must be a literal"
    labelled = set(re.findall(r"(\w+):\s*\"", labels.group(1)))
    assert labelled == set(overlays), (
        f"unlabelled: {set(overlays) - labelled}; extra: {labelled - set(overlays)}"
    )


def test_every_doc_kind_is_reachable_from_the_registry() -> None:
    """An addressable doc kind that no verb can open is an orphan the URL
    can reach but the user cannot."""
    kinds = _mini_racer_value(FOUNDATION / "shell-url.js", "SH_DOC_KINDS")
    text = _all_shell_text()
    openers = set(re.findall(r'openDoc\(\{\s*kind:\s*"(\w+)"', text))
    missing = set(kinds) - openers
    assert not missing, f"doc kinds no verb opens: {sorted(missing)}"


def test_every_chorded_verb_id_exists() -> None:
    text = _all_shell_text()
    chords = re.search(r"var SH_CHORDS = \{([\s\S]*?)\n\};", text)
    assert chords, "SH_CHORDS must be a literal map"
    wanted = set(re.findall(r':\s*"([\w.]+)"', chords.group(1)))
    registered = {e["id"] for e in _registrations() if "id" in e}
    # Generated overlay verbs are not literal ids in the source.
    overlays = _mini_racer_value(FOUNDATION / "shell-url.js", "SH_OVERLAYS")
    registered |= {"overlay.open." + name for name in overlays}
    missing = wanted - registered
    assert not missing, f"chords bound to unregistered verbs: {sorted(missing)}"


def test_every_data_verb_attribute_names_a_registered_verb() -> None:
    """The pointer affordances render data-verb="<id>"; a typo there is a
    button that looks live and does nothing."""
    referenced = set(re.findall(r'data-verb="([\w.]+)"', _all_shell_text()))
    registered = {e["id"] for e in _registrations() if "id" in e}
    missing = referenced - registered
    assert not missing, f"data-verb pointing at nothing: {sorted(missing)}"
