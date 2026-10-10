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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import CDPSession, Page

# The standing sweep's allowlist: (a regex searched in the control's outer HTML, why it cannot be named: a ticket, or the reason). It is EMPTY on purpose and may only shrink:
# an entry that matches nothing in a run fails the sweep (the control was fixed or removed, so the entry has to go), and tests/ui/test_a11y_sweep_classifier.py pins it empty.
ALLOWLIST: list[tuple[str, str]] = []

# How many characters of a control's collapsed outer HTML the report shows and the allowlist is matched against (a pattern should name a stable attribute near the start, e.g. a data-testid).
HTML_WINDOW = 220

PROBE_ATTRIBUTE = "data-a11y-probe"

CANDIDATES_JS = r"""(args) => {
  const { rootSel, window: win, attr, chrome } = args;
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
      return { html, testid: e.getAttribute('data-testid') || '', editable, body: !(chrome && e.closest(chrome)) };
    }),
  };
}"""

CLEAR_JS = "(attr) => document.querySelectorAll('[' + attr + ']').forEach(e => e.removeAttribute(attr))"

# What a surface says about ITSELF, the second line of defence (review of #668, B3' and round 3 B2''): the first is the network (``api_problem``: a /v1 response of 500 or more, a request that
# failed), because the console draws "this page failed" in many ways and no list of them is complete. A control count is met by a page's own chrome, so the sweep also asks whether anything under
# the root is still loading and whether it shows an error banner. RECOGNISED here: the console's banners and form errors (``.nv-form-error``, ``.banner-error``, ``.nv-doc-problem``,
# ``[role=alert]`` with text), the activity feed's failure (``.sh-file-conflict``), a hint line that says it could not load something (``.field-help.warn`` whose text has "couldn't", "could not",
# "failed" or "unable": the same class draws the harmless "Admin only" hints), a red empty state (``.nv-bind-empty`` coloured ``--red``); a spinner, an element marked busy, and text that starts Loading, Checking or Reading (any case) or is only an ellipsis. NOT recognised by shape, and so left to the network guard:
# a red span in a table row (toolsets), a stuck title (health), a provider class body that says "No providers match" while its list loads.
ERROR_BANNER = '.nv-form-error, .banner-error, .nv-doc-problem, [role="alert"], .sh-file-conflict, .nv-bind-empty[style*="--red"]'
WARNING_HINT = ".field-help.warn"
FAILURE_WORDS = r"couldn.?t|could not|failed|unable"
LOADING_TEXT = r"^(loading|checking|reading)\b|^\u2026$"

PAGE_STATE_JS = r"""(args) => {
  const { rootSel, errorSel, warnSel, failureWords, loadingText } = args;
  const shown = e => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && e.getClientRects().length > 0; };
  const roots = rootSel ? Array.from(document.querySelectorAll(rootSel)).filter(shown) : [document.documentElement];
  if (!roots.length) return { error: 'the root selector ' + JSON.stringify(rootSel) + ' matches no visible element', loading: [], errors: [] };
  const text = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
  const own = e => Array.from(e.childNodes).filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
  const rx = new RegExp(loadingText, 'i');
  const failed = new RegExp(failureWords, 'i');
  const loading = [], errors = [];
  const seen = new Set();
  for (const root of roots) {
    for (const e of [root, ...root.querySelectorAll('*')]) {
      if (seen.has(e) || !shown(e)) continue;
      seen.add(e);
      if (e.classList.contains('spinner')) loading.push(e.tagName.toLowerCase() + '.spinner');
      else if (e.getAttribute('aria-busy') === 'true') loading.push(e.tagName.toLowerCase() + '[aria-busy=true]');
      else if (rx.test(own(e))) loading.push(own(e).slice(0, 80));
      if (e.matches(errorSel) && text(e) && !e.parentElement.closest(errorSel)) errors.push(text(e).slice(0, 120));
      else if (e.matches(warnSel) && failed.test(text(e))) errors.push(text(e).slice(0, 120));
    }
  }
  return { error: null, loading, errors };
}"""


_INVISIBLE = re.compile(r"[\s\u200b-\u200d\u2060\ufeff]+")


def effective_source_entry(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """The name source that produced the name, per Chromium's own list: the first one that produced a value and was not superseded by a higher-priority one."""
    for source in sources or []:
        value = (source.get("value") or {}).get("value")
        if value and not source.get("superseded") and not source.get("invalid"):
            return source
    return {}


def effective_source(sources: list[dict[str, Any]]) -> str:
    """What the accessible name came from (``attribute``, ``contents``, ``labelfor`` ...), or ``""`` when no source produced one."""
    entry = effective_source_entry(sources)
    return str(entry.get("nativeSource") or entry.get("type") or "") if entry else ""


def _is_named_by_its_own_placeholder_through_itself(ax: dict[str, Any], sources: list[dict[str, Any]], name: str) -> bool:
    """``aria-labelledby`` that points at the control itself (and at nothing that has text) makes Chromium read the placeholder as the name, from a source that looks like a label."""
    entry = effective_source_entry(sources)
    if entry.get("attribute") != "aria-labelledby" or ax.get("backendDOMNodeId") is None:
        return False
    related = [node for node in ((entry.get("attributeValue") or {}).get("relatedNodes") or []) if str(node.get("text") or "").strip()]
    if not related or any(node.get("backendDOMNodeId") != ax["backendDOMNodeId"] for node in related):
        return False
    placeholders = [(source.get("value") or {}).get("value") for source in sources if source.get("type") == "placeholder" and source.get("attribute") == "placeholder"]
    return name in [str(value) for value in placeholders if value]


def verdict(ax: dict[str, Any] | None) -> tuple[str, str]:
    """``(outcome, why)`` for one control, from its node in Chromium's accessibility tree. ``outcome`` is ``skipped`` (not exposed to assistive technology), ``named`` or ``unnamed``."""
    if not ax or ax.get("ignored"):
        return "skipped", "not in the accessibility tree"
    name_value = ax.get("name") or {}
    name = _INVISIBLE.sub("", str(name_value.get("value") or ""))
    if not name:
        return "unnamed", "no accessible name"
    sources = name_value.get("sources") or []
    source = effective_source(sources)
    if source == "placeholder":
        return "unnamed", "its only name is its placeholder"
    if _is_named_by_its_own_placeholder_through_itself(ax, sources, str(name_value.get("value") or "")):
        return "unnamed", "its only name is its placeholder (its aria-labelledby points at itself)"
    return "named", source or "name"


@dataclass
class Examination:
    """What one look at a page found: how many controls were examined (a sweep that examined none proved nothing), how many of those are in the BODY (outside the ``chrome`` the caller named),
    how many Chromium hides from assistive technology, and the unnamed ones."""

    examined: int = 0
    body: int = 0
    skipped: int = 0
    unnamed: list[dict[str, str]] = field(default_factory=list)

    @property
    def html(self) -> list[str]:
        return [item["html"] for item in self.unnamed]


class _Stale(Exception):
    """A candidate left the page between the moment it was marked and the moment the browser was asked about it."""


class AxProbe:
    """Chromium's accessibility tree for the candidate controls of a page, over one CDP session."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.cdp: CDPSession = page.context.new_cdp_session(page)
        self.cdp.send("Accessibility.enable")

    def close(self) -> None:
        self.cdp.detach()

    def examine(self, root: str | None = None, *, chrome: str | None = None, _after_marking: Callable[[], None] | None = None) -> Examination:
        """Examine every candidate control under ``root`` (a CSS selector; the whole document when none). Raises when ``root`` matches no visible element: a sweep must not fall back to the page.

        ``chrome`` is a selector for the fixed furniture around a surface (a modal's title and footer, an overlay's close button ...): its controls are examined for names like any other and
        are not counted in ``body``. A page that changes under the probe (a list that polls) is looked at again, once; ``_after_marking`` is a seam for the test that makes it change."""
        try:
            return self._examine_once(root, chrome, _after_marking)
        except _Stale:
            pass
        try:
            return self._examine_once(root, chrome, _after_marking)
        except _Stale as exc:
            raise AssertionError(f"the page kept changing under the probe: {exc}") from exc

    def _examine_once(self, root: str | None, chrome: str | None, after_marking: Callable[[], None] | None) -> Examination:
        from playwright.sync_api import Error as BrowserError

        found = self.page.evaluate(CANDIDATES_JS, {"rootSel": root, "window": HTML_WINDOW, "attr": PROBE_ATTRIBUTE, "chrome": chrome})
        try:
            if found["error"]:
                raise AssertionError(found["error"])
            candidates = found["candidates"]
            out = Examination()
            if not candidates:
                return out
            if after_marking:
                after_marking()
            try:
                document = self.cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
                for index, candidate in enumerate(candidates):
                    # each candidate is found by the mark it carries, so that a control added or removed since the enumeration cannot shift the answers onto its neighbours
                    node_id = self.cdp.send("DOM.querySelector", {"nodeId": document, "selector": f'[{PROBE_ATTRIBUTE}="{index}"]'})["nodeId"]
                    if not node_id:
                        raise _Stale(f"candidate {index} ({candidate['html'][:60]!r}) left the page")
                    nodes = self.cdp.send("Accessibility.getPartialAXTree", {"nodeId": node_id, "fetchRelatives": False})["nodes"]
                    ax = nodes[0] if nodes else None
                    outcome, why = verdict(ax)
                    if outcome == "skipped":
                        out.skipped += 1
                        continue
                    out.examined += 1
                    out.body += 1 if candidate["body"] else 0
                    if outcome == "unnamed":
                        role = ((ax or {}).get("role") or {}).get("value") or ""
                        out.unnamed.append({"html": candidate["html"], "testid": candidate["testid"], "role": role, "why": why})
            except BrowserError as exc:
                raise _Stale(str(exc)) from exc
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


def is_event_stream(resource_type: str, content_type: str | None) -> bool:
    """An event stream (the tap's ``EventSource``, a streamed reply) stays open for as long as the page does: it is neither "in flight" nor a failure."""
    return resource_type == "eventsource" or "text/event-stream" in (content_type or "")


def api_problem(*, method: str, url: str, resource_type: str, status: int | None = None, failure: str | None = None) -> str | None:
    """What a request of the page says about its surface, or ``None``: a ``/v1`` response of 500 or more, or a ``/v1`` request that failed. A 4xx is often the page's normal answer (an empty install
    has nothing to show), ``net::ERR_ABORTED`` is the console cancelling a poll of a page it left, and an event stream is not the API's business."""
    if "/v1/" not in url or resource_type == "eventsource":
        return None
    label = f"{method} /v1/" + url.split("/v1/", 1)[1].split("?", 1)[0]
    if failure is not None:
        return None if "ERR_ABORTED" in failure else f"{label} failed ({failure})"
    if status is not None and status >= 500:
        return f"{label} answered {status}"
    return None


class SweepDeadlineExceeded(Exception):
    """The sweep ran past its wall-clock deadline. An ordinary exception, raised by ``Budget.check`` between surfaces and by ``Budget.wait_ms`` before any wait (Playwright treats ``timeout=0`` as no
    timeout, so a wait is never handed out after the deadline): the test's own ``finally`` runs with a live browser, what was found is reported and the seeds are deleted. (A signal timeout is not: its
    ``Failed`` is raised inside Playwright's dispatcher greenlet, and every later sync call spins.)"""


class Budget:
    """The sweep-wide limit on waiting for stuck surfaces: 69 looks of 10 s and 40 forms of 25 s are ~1,700 s, more than the test may take, and a thread timeout kills the whole lane. After ``limit``
    looks that used their wait up, later waits are ``short_ms`` (a stuck look is still noted, it is just not waited out). With a ``deadline_s`` the sweep is bounded from inside: the clock starts at
    the first wait (or the first ``check``: the test makes one before its seeds), no wait runs past the deadline, and past it both ``wait_ms`` and ``check`` raise ``SweepDeadlineExceeded``. ``wait_ms``
    must raise and not return 0: Playwright treats ``timeout=0`` as no timeout at all, so a 0 handed out after the deadline waits for ever. ``clock`` is for a test."""

    def __init__(self, limit: int = 5, short_ms: int = 1000, deadline_s: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        if short_ms <= 0:
            raise ValueError(f"short_ms must be positive (Playwright treats a timeout of 0 as none), not {short_ms}")
        self.limit = limit
        self.short_ms = short_ms
        self.deadline_s = deadline_s
        self.clock = clock
        self.used = 0
        self._started: float | None = None

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def _remaining_ms(self) -> int | None:
        if self.deadline_s is None:
            return None
        now = self.clock()
        if self._started is None:
            self._started = now
        return max(0, int((self._started + self.deadline_s - now) * 1000))

    @property
    def expired(self) -> bool:
        remaining = self._remaining_ms()
        return remaining is not None and remaining <= 0

    def spent(self) -> None:
        self.used += 1

    def wait_ms(self, normal_ms: int) -> int:
        if normal_ms <= 0:
            raise ValueError(f"a wait must be positive (Playwright treats a timeout of 0 as none), not {normal_ms}")
        wait = min(normal_ms, self.short_ms) if self.exhausted else normal_ms
        remaining = self._remaining_ms()
        if remaining is None:
            return wait
        if remaining <= 0:
            raise SweepDeadlineExceeded(f"the sweep ran past its {self.deadline_s} s deadline before a wait of {normal_ms} ms")
        return min(wait, remaining)

    def check(self, surface: str) -> None:
        if self.expired:
            raise SweepDeadlineExceeded(f"the sweep ran past its {self.deadline_s} s deadline before {surface!r}")


@dataclass
class PageState:
    """What a surface says about itself: what under it is still loading, and the error banners it shows."""

    loading: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def page_state(page: Page, root: str | None = None) -> PageState:
    """Look for loading and error states under ``root`` (a CSS selector that must match a visible element; the whole document when none)."""
    found = page.evaluate(PAGE_STATE_JS, {"rootSel": root, "errorSel": ERROR_BANNER, "warnSel": WARNING_HINT, "failureWords": FAILURE_WORDS, "loadingText": LOADING_TEXT})
    if found["error"]:
        raise AssertionError(found["error"])
    return PageState(loading=found["loading"], errors=found["errors"])


@dataclass
class Look:
    """What the sweep recorded for one surface: the controls examined, how many of them are in its body (outside the fixed chrome), and how many Chromium hid from assistive technology."""

    examined: int = 0
    body: int = 0
    skipped: int = 0


def counts_table(looks: dict[str, Look], floors: dict[str, int]) -> str:
    """The sweep's numbers, one line per surface, to print on every run: floors are calibrated from them, and a page that examined little shows here."""
    width = max([len(name) for name in looks] + [len("surface")])
    lines = [f"{'surface'.ljust(width)}  {'examined':>8}  {'body':>5}  {'floor':>5}  {'skipped':>7}"]
    for name, look in looks.items():
        floor = floors.get(name)
        lines.append(f"{name.ljust(width)}  {look.examined:>8}  {look.body:>5}  {'-' if floor is None else floor:>5}  {look.skipped:>7}")
    return "\n".join(lines)


def evaluate_sweep(*, found: dict[str, list[str]], allowlist: list[tuple[str, str]], visited: list[str], expected: list[str], looks: dict[str, Look], floors: dict[str, int],
                   notes: list[str], page_errors: list[str], left: list[str], completed: bool = True) -> list[str]:
    """Every guard at the end of the standing sweep, as a pure function: the problems found (an empty list is a pass).

    ``found`` is the sweep's unnamed controls (outer HTML -> surfaces); ``visited`` the surfaces it looked at, in order, against ``expected``; ``looks`` what each look recorded, against the ``floors``
    on the BODY of each surface; ``notes`` what the pages said about themselves (still loading, an error banner, not the page asked for); ``left`` the seeded rows that could not be deleted.
    A sweep that died (``completed`` false) is not also blamed for the surfaces it never reached."""
    problems: list[str] = []
    unnamed, stale = classify(found, allowlist)
    if unnamed:
        report = "\n".join(f"  {', '.join(surfaces)}\n      {html}" for html, surfaces in unnamed.items())
        problems.append(f"{len(unnamed)} control(s) with no name, by surface:\n{report}")
    if completed and visited != expected:
        problems.append("the sweep did not visit exactly the surfaces it lists, difference: " + str(sorted(set(expected) ^ set(visited))) + f" ({len(visited)} visited, {len(expected)} listed)")
    thin = []
    for name, look in looks.items():
        if name not in floors:
            thin.append(f"{name}: no floor is set for it")
        elif look.body < floors[name]:
            thin.append(f"{name}: {look.body} control(s) in its body, at least {floors[name]} expected ({look.examined} examined with the chrome)")
    if thin:
        problems.append("surfaces that examined too few controls:\n  " + "\n  ".join(thin))
    if notes:
        problems.append("problems with the pages themselves:\n  " + "\n  ".join(notes))
    if page_errors:
        problems.append(f"page errors during the sweep: {page_errors}")
    if stale:
        problems.append(f"allowlist entries that match nothing in this run (remove them): {stale}")
    if left:
        problems.append(f"seeded rows that could not be deleted: {left}")
    return problems
