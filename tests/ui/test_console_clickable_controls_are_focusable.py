"""The console's click-drawn controls are real, focusable, named controls (console review C-003, the keyboard pass).

The Approvals page drew its only create affordance, "Add or edit one", as an ``<a>`` with an ``onClick`` and no ``href``: a link that is not a link, and no keyboard path to it either. Every Platform card (``nv-pcard``) carried its open action on the whole div: an ``onClick`` with no role or tabindex, so a keyboard user could not open a card (only its Delete button was reachable). The Platform and System nav rows, and the new-workspace overlay's template pick rows, are the same shape: click-only divs.

Each is a ``<button type="button">`` now: the approvals affordance (``approvals-config-link``), the Platform nav rows (``nv-plat-row``, ``aria-current="page"`` on the active one) and the System nav rows one file over (``nv-sys-row``, the same class), the template pick rows (``nv-pick-row``), and the card's open action, which moved from the card div to the ``Open`` button inside it (``nv-pcard-open:<name>``, named ``Open <name>``); a click anywhere on the card still opens it, because the button is stretched over the whole card (``.nv-pcard`` is ``position: relative`` and ``.nv-pcard-open::after`` is ``position: absolute; inset: 0``), with the Delete button above it (``z-index: 1``), and the Open label is fully opaque at rest (no opacity rule at all).

Static pins on each site, in the slicing style of the rest of ``tests/ui``: a native button, ``type="button"``, no ``tabIndex`` (a button with ``tabIndex={-1}`` is unreachable by Tab, which is how a keyboard regression would land), the Open button's ``onClick`` and its name, the stretched CSS rules, the phone's content-sized System chips, and no ``opacity`` in the Open's rule. The browser half is ``tests/ui_e2e/test_platform_overlay_close_refetch_journey.py`` (opens a card from a click on its name) and ``tests/ui_e2e/test_platform_filters_and_inputs_named_journey.py`` (sweeps the Platform view for unnamed controls).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"
CSS = ROOT / "ui" / "styles.css"

_OPENER_RE = re.compile(r"<(?:button|a|div|span)(?=[\s>/])")
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")


def _tag(src: str, anchor: str) -> tuple[int, str]:
    """The start position and the whole opening tag that contains ``anchor`` (a unique piece of it).

    The rightmost opener before the anchor is the enclosing one: an opening tag's attribute list cannot hold another opener, and an opener written with a newline after the tag name (``<a\n``) matches as well as one with a space.
    """
    at = src.index(anchor)
    starts = [m.start() for m in _OPENER_RE.finditer(src[: at + 1])]
    start = max(starts)
    depth, quote, i = 0, "", start
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth <= 0:
            return start, src[start:i + 1]
        i += 1
    raise AssertionError(anchor)


def _inner_text(src: str, tag_start: int) -> str:
    """The visible text between the opening tag at ``tag_start`` and the next ``</button>``.

    A JSX ``{...}`` expression is skipped whole (it may hold a ``<`` or a ``=>`` of its own), so only real tags are stripped.
    """
    end = src.index("</button>", tag_start)
    inner = src[tag_start:end]
    out = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "{":
            depth, j = 0, i
            while j < len(inner):
                if inner[j] == "{":
                    depth += 1
                elif inner[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
        elif c == "<":
            m = _TAG_RE.match(inner, i)
            i = m.end() if m else i + 1
        else:
            out.append(c)
            i += 1
    return "".join(out).strip()


def _src(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def _css(selector: str) -> str:
    """The body of the first top-level ``selector { ... }`` rule of styles.css (comments stripped: a commented-out rule is not a rule)."""
    src = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    m = re.search(r"^" + re.escape(selector) + r"\s*\{", src, re.MULTILINE)
    assert m, f"no top-level rule for {selector!r}"
    i = m.end() - 1
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i + 1:j]


def test_the_approvals_create_affordance_is_a_named_button() -> None:
    src = _src("approvals.jsx")
    start, tag = _tag(src, 'data-testid="approvals-config-link"')
    assert tag.startswith("<button"), f"the affordance is not a native button:\n{tag}"
    assert 'type="button"' in tag, tag
    assert "tabIndex" not in tag, f"the affordance is out of the tab order:\n{tag}"
    assert "onClick={() => onConfigure && onConfigure()}" in tag, f"the button does not open the configuration:\n{tag}"
    assert 'border: "none"' in tag, f"the button draws the UA border:\n{tag}"
    assert "Add or edit one" in _inner_text(src, start), "the button has no words a screen reader could read"


def test_the_platform_card_opens_from_a_button_inside_the_card() -> None:
    src = _src("console/nv-platform.jsx")
    start, tag = _tag(src, "nv-pcard-open")
    assert tag.startswith("<button"), f"the card's open action is not a native button:\n{tag}"
    assert 'type="button"' in tag, tag
    assert "tabIndex" not in tag, f"the Open button is out of the tab order:\n{tag}"
    assert "onClick={props.onOpen}" in tag, f"the Open button does not open the card:\n{tag}"
    assert 'aria-label={"Open " + c.name}' in tag, f"the Open button is not named after the card:\n{tag}"
    assert "Open" in _inner_text(src, start), "the button has no words a screen reader could read"


def test_the_platform_card_still_opens_from_anywhere_on_it_via_the_stretched_button() -> None:
    """The card div carries no click of its own: the Open button's ::after is stretched over the whole card, so a click anywhere on it lands on the button, and the Delete button sits above that stretch."""
    _, card = _tag(_src("console/nv-platform.jsx"), 'className="nv-pcard"')
    assert "onClick" not in card, f"the whole card is still the click target:\n{card}"
    pcard = _css(".nv-pcard")
    assert "position: relative" in pcard, f"the stretch has no containing block:\n{pcard}"
    after = _css(".nv-pcard-open::after")
    for decl in ('content: ""', "position: absolute"):
        assert decl in after, f".nv-pcard-open::after does not stretch the Open over the card (missing {decl!r}):\n{after}"
    assert re.search(r"\binset:\s*0\s*;", after), f"the stretch does not cover the whole card:\n{after}"
    assert "pointer-events" not in after, f"a dropped click on the stretch leaves the card unopenable:\n{after}"
    assert "pointer-events" not in _css(".nv-pcard-open"), "the ::after inherits pointer-events from the Open button"
    dele = _css(".nv-pcard-del")
    assert "position: relative" in dele and "z-index: 1" in dele, f"the Delete button is not above the stretched Open:\n{dele}"
    assert "pointer-events" not in dele, f"a dropped click on Delete lands on the stretched Open and opens the card:\n{dele}"


def test_the_platform_nav_rows_are_focusable_buttons() -> None:
    src = _src("console/nv-platform.jsx")
    start, tag = _tag(src, 'data-testid={"nv-plat-row:" + id}')
    assert tag.startswith("<button"), f"the Platform nav row is not a native button:\n{tag}"
    assert 'type="button"' in tag, tag
    assert "tabIndex" not in tag, f"the nav row is out of the tab order:\n{tag}"
    assert 'aria-current={id === active ? "page" : undefined}' in tag, f"the active nav row is not marked for assistive tech:\n{tag}"


def test_the_system_nav_rows_are_focusable_buttons() -> None:
    """The System view draws the same click-only rows one file over, on the same nv-plat-row class."""
    src = _src("console/nv-system.jsx")
    start, tag = _tag(src, 'data-testid={"nv-sys-row:" + id}')
    assert tag.startswith("<button"), f"the System nav row is not a native button:\n{tag}"
    assert 'type="button"' in tag, tag
    assert "tabIndex" not in tag, f"the nav row is out of the tab order:\n{tag}"
    assert 'aria-current={id === nav ? "page" : undefined}' in tag, f"the active nav row is not marked for assistive tech:\n{tag}"


def test_the_template_pick_rows_are_focusable_buttons() -> None:
    src = _src("console/nv-overlays.jsx")
    start, tag = _tag(src, 'data-testid={"nv-nw-tpl:" + t.id}')
    assert tag.startswith("<button"), f"the template pick row is not a native button:\n{tag}"
    assert 'type="button"' in tag, tag
    assert "tabIndex" not in tag, f"the pick row is out of the tab order:\n{tag}"
    assert "aria-pressed={t.id === tplId}" in tag, tag


def test_the_nav_rows_keep_their_borderless_button_reset() -> None:
    """A <button> without border: none would draw a UA border the click-only div rows never had; a row that does not fill its column would not either."""
    plat_row = _css(".nv-plat-row")
    assert "border: none" in plat_row, plat_row
    assert "width: 100%" in plat_row, f"the row must fill its column like the div it replaced:\n{plat_row}"
    assert "background: transparent" in plat_row, plat_row


def test_the_system_nav_chips_are_content_sized_on_the_phone() -> None:
    """On the phone (More > System settings) the nav is a row of chips, and width: 100% would make every chip as wide as the strip."""
    assert "width: auto" in _css(".nv-mob-system-body .nv-plat-row")


def test_the_open_label_is_fully_opaque_at_rest() -> None:
    """At rest it was 0.55 (2.21:1); --accent on --bg-1 is pinned in test_token_contrast.py."""
    assert "opacity" not in _css(".nv-pcard-open")
