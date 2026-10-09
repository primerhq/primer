"""One definition of "a visible control with no accessible name", for the journeys and the standing sweep that look at the REAL page.

The NAME COMES FROM CHROMIUM. A DOM heuristic (``aria-label``, ``<label>``, text, ``title`` ...) disagreed with the browser in both directions: it passed a button whose only text is
``aria-hidden``, a checkbox with ``role=switch`` whose ``value`` is "on", an icon-only ``<a href>`` and a ``contenteditable`` editor, all of which Chromium names ``""``, and it flagged
controls Chromium does name. So the page only ENUMERATES the candidate controls (``CANDIDATES_JS``) and Chromium's accessibility tree says what each one is called
(``Accessibility.getPartialAXTree`` over CDP); ``verdict`` is the pure rule applied to what it answers:

* a control that is not in the accessibility tree (``ignored``: ``aria-hidden``, ``inert`` ancestry, ``role=none``) is skipped;
* an empty name is unnamed, and so is one made only of whitespace, non-breaking spaces and zero-width characters;
* a name whose only source is the ``placeholder`` is unnamed: it vanishes when the user types and is not read as the field's name by every screen reader. Every other source counts, as
  Chromium computes it (``aria-labelledby``, ``aria-label``, a ``<label>``, the element's text or ``alt``, an SVG ``<title>``, and ``title``: a control named only by its ``title`` is named).

The candidates are ``input``, ``select``, ``textarea``, ``button``, ``summary``, ``a[href]``, ``[contenteditable]`` (the editing host, which must be named whatever role it has) and the
control roles (``button``, ``link``, ``textbox``, ``searchbox``, ``combobox``, ``listbox``, ``option``, ``checkbox``, ``radio``, ``switch``, ``slider``, ``spinbutton``, ``tab``, ``menuitem``,
``menuitemcheckbox``, ``menuitemradio``, ``treeitem``), including a control of zero size that is still focusable. A control is a candidate when it is displayed and not under
``aria-hidden`` or ``inert``. What this cannot see (a ``div`` that only has a click handler) is listed in ``docs/dev/subsystems/ui-pages.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import CDPSession, Page

# The standing sweep's allowlist: (a regex searched in the control's outer HTML, why it cannot be named: a ticket, or the reason). It is EMPTY on purpose and may only shrink:
# an entry that matches nothing in a run fails the sweep (the control was fixed or removed, so the entry has to go), and tests/ui/test_a11y_sweep_classifier.py pins it empty.
ALLOWLIST: list[tuple[str, str]] = []

# How many characters of a control's collapsed outer HTML the report shows and the allowlist is matched against (a pattern should name a stable attribute near the start, e.g. a data-testid).
HTML_WINDOW = 220

PROBE_ATTRIBUTE = "data-a11y-probe"

CANDIDATES_JS = r"""(args) => {
  const { rootSel, window: win, attr } = args;
  const shown = e => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && e.getClientRects().length > 0; };
  const roots = rootSel ? Array.from(document.querySelectorAll(rootSel)).filter(shown) : [document.documentElement];
  if (!roots.length) return { error: 'the root selector ' + JSON.stringify(rootSel) + ' matches no visible element', candidates: [] };
  const ROLES = ['button', 'link', 'textbox', 'searchbox', 'combobox', 'listbox', 'option', 'checkbox', 'radio', 'switch', 'slider', 'spinbutton', 'tab',
                 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'treeitem'];
  const SELECTOR = 'input, select, textarea, button, summary, a[href], [contenteditable], [role]';
  const NATIVE = ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'SUMMARY'];
  const seen = new Set();
  const found = [];
  for (const root of roots) {
    for (const e of root.querySelectorAll(SELECTOR)) {
      if (seen.has(e)) continue;
      seen.add(e);
      const role = (e.getAttribute('role') || '').trim().split(/\s+/)[0];
      const editable = e.isContentEditable && e.getAttribute('contenteditable') !== 'false' && !(e.parentElement && e.parentElement.isContentEditable);
      const native = NATIVE.includes(e.tagName) || (e.tagName === 'A' && e.hasAttribute('href'));
      if (!native && !editable && !ROLES.includes(role)) continue;
      if (e.tagName === 'INPUT' && e.type === 'hidden') continue;
      if (!shown(e) || e.closest('[aria-hidden="true"], [inert]')) continue;
      found.push({ e, editable });
    }
  }
  found.sort((a, b) => (a.e.compareDocumentPosition(b.e) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  return {
    error: null,
    candidates: found.map(({ e, editable }, i) => {
      const html = e.outerHTML.replace(/\s+/g, ' ').slice(0, win);   // before the mark goes on: the report must be the page's own markup
      e.setAttribute(attr, String(i));
      return { html, testid: e.getAttribute('data-testid') || '', editable };
    }),
  };
}"""

CLEAR_JS = "(attr) => document.querySelectorAll('[' + attr + ']').forEach(e => e.removeAttribute(attr))"

_INVISIBLE = re.compile(r"[\s​-‍⁠﻿]+")


def effective_source(sources: list[dict[str, Any]]) -> str:
    """What the accessible name came from, per Chromium's own list of name sources: the first one that produced a value and was not superseded by a higher-priority one."""
    for source in sources or []:
        value = (source.get("value") or {}).get("value")
        if value and not source.get("superseded") and not source.get("invalid"):
            return str(source.get("nativeSource") or source.get("type") or "")
    return ""


def verdict(ax: dict[str, Any] | None) -> tuple[str, str]:
    """``(outcome, why)`` for one control, from its node in Chromium's accessibility tree. ``outcome`` is ``skipped`` (not exposed to assistive technology), ``named`` or ``unnamed``."""
    if not ax or ax.get("ignored"):
        return "skipped", "not in the accessibility tree"
    name_value = ax.get("name") or {}
    name = _INVISIBLE.sub("", str(name_value.get("value") or ""))
    if not name:
        return "unnamed", "no accessible name"
    source = effective_source(name_value.get("sources") or [])
    if source == "placeholder":
        return "unnamed", "its only name is its placeholder"
    return "named", source or "name"


@dataclass
class Examination:
    """What one look at a page found: how many controls were examined (a sweep that examined none proved nothing) and the unnamed ones."""

    examined: int = 0
    skipped: int = 0
    unnamed: list[dict[str, str]] = field(default_factory=list)

    @property
    def html(self) -> list[str]:
        return [item["html"] for item in self.unnamed]


class AxProbe:
    """Chromium's accessibility tree for the candidate controls of a page, over one CDP session."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.cdp: CDPSession = page.context.new_cdp_session(page)
        self.cdp.send("Accessibility.enable")

    def close(self) -> None:
        self.cdp.detach()

    def examine(self, root: str | None = None) -> Examination:
        """Examine every candidate control under ``root`` (a CSS selector; the whole document when none). Raises when ``root`` matches no visible element: a sweep must not fall back to the page."""
        found = self.page.evaluate(CANDIDATES_JS, {"rootSel": root, "window": HTML_WINDOW, "attr": PROBE_ATTRIBUTE})
        try:
            if found["error"]:
                raise AssertionError(found["error"])
            candidates = found["candidates"]
            out = Examination()
            if not candidates:
                return out
            document = self.cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
            ids = self.cdp.send("DOM.querySelectorAll", {"nodeId": document, "selector": f"[{PROBE_ATTRIBUTE}]"})["nodeIds"]
            assert len(ids) == len(candidates), f"{len(candidates)} candidates were marked, {len(ids)} found by the browser"
            for candidate, node_id in zip(candidates, ids):
                nodes = self.cdp.send("Accessibility.getPartialAXTree", {"nodeId": node_id, "fetchRelatives": False})["nodes"]
                ax = nodes[0] if nodes else None
                outcome, why = verdict(ax)
                if outcome == "skipped":
                    out.skipped += 1
                    continue
                out.examined += 1
                if outcome == "unnamed":
                    role = ((ax or {}).get("role") or {}).get("value") or ""
                    out.unnamed.append({"html": candidate["html"], "testid": candidate["testid"], "role": role, "why": why})
            return out
        finally:
            self.page.evaluate(CLEAR_JS, PROBE_ATTRIBUTE)


def unnamed_controls(page: Page, root: str | None = None) -> list[str]:
    """The outer HTML (first ``HTML_WINDOW`` characters) of every visible control under ``root`` (a CSS selector; the whole page when none) that Chromium gives no accessible name."""
    probe = AxProbe(page)
    try:
        return probe.examine(root).html
    finally:
        probe.close()


def classify(found: dict[str, list[str]], allowlist: list[tuple[str, str]]) -> tuple[dict[str, list[str]], list[str]]:
    """Split a sweep's findings (outer HTML -> the surfaces it was seen on) into the controls the allowlist does not excuse and the allowlist patterns that excused nothing.

    Every entry that matches a control is credited with it, not only the first (an entry that is shadowed by an earlier, broader one is not stale because of the order of the list)."""
    allowed = [re.compile(pattern) for pattern, _reason in allowlist]
    used: set[int] = set()
    unnamed: dict[str, list[str]] = {}
    for html, surfaces in found.items():
        hits = [i for i, rx in enumerate(allowed) if rx.search(html)]
        used.update(hits)
        if not hits:
            unnamed[html] = sorted(set(surfaces))
    stale = [allowlist[i][0] for i in range(len(allowlist)) if i not in used]
    return unnamed, stale
