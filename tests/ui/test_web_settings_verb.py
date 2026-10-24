"""The palette verb that opens Platform › Web (platform revamp 04, Task 5).

The Web page is a custom Platform page (seam I-2): ``NV_platVerbSpecs`` leaves it out, and the shell registers its verb itself, right after the Platform
loop of the core-verb effect (``nv-shell.jsx``). The verb is ``platform.open.web`` (the id the Platform sidebar row already renders as its ``data-verb``, so
the row is its pointer affordance), labelled "Open Web Settings" with the aliases "Web Search" and "Web Fetch" (the plan's copy table), and it opens
``view=platform:web``. Like the row, it exists only while the page's component is loaded: a verb for a page that is not there would land on Providers.

The shell's own effect runs here against the real registry and the real ranker (``shell-verbs.js``), with ``goView`` recorded, in the Platform page
harness that ``tests/ui/test_platform_verbs.py`` uses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._platform_page_harness import platform_page_context
from tests.ui.test_shell_dual_render_guard import _all_console_text, _console_sources, _registrations, _verbs_without_a_pointer_affordance

ROOT = Path(__file__).resolve().parents[2]
SHELL_URL = (ROOT / "ui" / "foundation" / "shell-url.js").read_text(encoding="utf-8")
SHELL_VERBS = (ROOT / "ui" / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")

VERB = "platform.open.web"
WEB_PAGE_STAND_IN = "window.WEB_SettingsPage = function () { return null; };"


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _effect_source() -> str:
    start = SHELL.index("// --- Core verbs (chrome-level; view bodies add their own)")
    body = SHELL.index("React.useEffect(function () {", start) + len("React.useEffect(function () {")
    return SHELL[body:SHELL.index("}, [registry]);", body)]


def _run_effect(ctx) -> None:
    """The shell's core-verb effect as ``nv-shell.jsx`` writes it, against the real registry; ``goView`` records its arguments into ``VIEWS``."""
    ctx.eval(
        "var registry = SH_createVerbRegistry(); var VIEWS = []; var WARNS = []; console.warn = function (m) { WARNS.push(String(m)); };"
        "(function (registry, goView) {" + _effect_source() + "\n})(registry, function () { VIEWS.push([].slice.call(arguments)); });"
    )


@pytest.fixture
def make():
    made = []

    def _make(*, page_loaded: bool = True):
        ctx = platform_page_context(prelude_extra=SHELL_URL + SHELL_VERBS + (WEB_PAGE_STAND_IN if page_loaded else ""))
        made.append(ctx)
        _run_effect(ctx)
        return ctx

    yield _make
    for ctx in made:
        ctx.close()


# The context the palette ranks with for an admin (nv-palette.jsx passes the signed-in role): every registered verb is offered to an admin, while an
# admin-only verb (Reset Prime Instructions, pivot P2 Task 15) is hidden from any other role (tests/ui/test_reset_prime_instructions_action.py).
def _ranked(ctx, query: str) -> list[str]:
    return js(ctx, "SH_rankVerbs(registry, " + json.dumps(query) + ', { role: "admin" }).map(function (v) { return v.id; })')


def test_the_shell_registers_open_web_settings() -> None:
    """The registration literal: on the palette and the Platform sidebar, with a label the registry's lint accepts on a surface it knows."""
    entries = [e for e in _registrations() if e.get("id") == VERB]
    assert len(entries) == 1, "one registration of the Web page's verb"
    surfaces = set(re.findall(r'"([\w-]+)"', entries[0]["surfaces"]))
    assert surfaces == {"palette", "platform-nav"}
    ctx = platform_page_context(prelude_extra=SHELL_VERBS)
    try:
        assert js(ctx, 'SH_lintVerbLabel("Open Web Settings")') is None
        assert "platform-nav" in js(ctx, "SH_SURFACES")
    finally:
        ctx.close()


def test_the_sidebar_row_is_its_pointer_affordance() -> None:
    """``NV_PlatNav`` draws ``data-verb={"platform.open." + id}`` on every row, the Web row included, so the verb is never palette-only."""
    entry = next(e for e in _registrations() if e.get("id") == VERB)
    assert _verbs_without_a_pointer_affordance([entry], _all_console_text(), _console_sources()["nv-platform.jsx"]) == []


def test_the_verb_opens_platform_web(make) -> None:
    ctx = make()
    verb = js(ctx, 'registry.get("' + VERB + '")')
    assert verb is not None, "registered while the page is loaded"
    assert verb["canonical"] == "Open Web Settings"
    assert verb["aliases"] == ["Web Search", "Web Fetch"]
    assert verb["label"] == "Open Web Settings (Web Search, Web Fetch)", "what the palette row shows"
    assert verb["surfaces"] == ["palette", "platform-nav"]
    ctx.eval('registry.get("' + VERB + '").run()')
    assert js(ctx, "VIEWS") == [["platform", "web"]]
    assert js(ctx, "WARNS") == []
    block = SHELL[SHELL.index('id: "' + VERB + '"'):]
    assert 'goView("platform", "web")' in block[:block.index("});")]


def test_without_the_page_there_is_no_verb(make) -> None:
    """The custom-page rule of the sidebar (no component, no row, and a link opens Providers) holds for the verb: no verb that would land somewhere else."""
    ctx = make(page_loaded=False)
    assert js(ctx, 'registry.get("' + VERB + '")') is None
    assert "platform.open.providers" in js(ctx, "registry.ids()"), "the other Platform verbs still register"


def test_it_registers_after_the_platform_loop_so_the_core_verbs_keep_the_empty_query(make) -> None:
    """The palette lists the first eight verbs of an empty query in registration order: those stay the core verbs."""
    ctx = make()
    ids = js(ctx, "registry.ids()")
    assert ids[-1] == VERB, "registered right after the Platform loop, last"
    assert VERB not in _ranked(ctx, "")[:8]
    loop = SHELL.index("window.NV_platVerbSpecs().forEach(")
    assert loop < SHELL.index('id: "' + VERB + '"') < SHELL.index("}, [registry]);", loop), "inside the effect that registers once, after the loop"


@pytest.mark.parametrize("query", ["Open Web Settings", "open web settings", "Web Search", "web search", "Web Fetch", "web fetch"])
def test_its_label_and_its_aliases_put_it_first(make, query) -> None:
    assert _ranked(make(), query)[0] == VERB


def test_every_verb_still_ranks_first_for_its_own_label(make) -> None:
    """With the Web verb in the population (its aliases name "Web Search"), every label, exact and in lower case, still puts its own verb first."""
    ctx = make()
    wrong = []
    for verb_id, label in js(ctx, "registry.all().map(function (v) { return [v.id, v.canonical]; })"):
        for query in (label, label.lower()):
            first = _ranked(ctx, query)[0]
            if first != verb_id:
                wrong.append(f"{query!r} puts {first} first, not {verb_id}")
    assert wrong == []
    assert "platform.open.providers.web_search" in _ranked(ctx, "Web Search")[1:], "the Providers › Web Search verb is still offered for the alias"
