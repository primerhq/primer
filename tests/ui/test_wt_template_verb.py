"""How the console reaches the Workspace templates page and its overlay (platform revamp Plan 08, Task 11).

* ``template.create`` ("Create Workspace Template", on the palette and on an overlay button): it opens ``view=platform:templates`` and asks the page to
  open its create overlay (``WT_requestCreate``). Its pointer affordance is the new-workspace dialog's "Create a template" button (a console file, where the
  dual-render guard looks) and the page's empty state.
* ``platform.open.templates`` ("Open Workspace Templates"): the custom page's own verb, registered after the shell's Platform loop as the custom-page rule
  says (``nv-shell.jsx``); the sidebar row is its affordance.
* Both exist only while the page's component is loaded (``NV_platNavAvailable("templates")``, main's rule for Web too): without the page, a link to it
  opens Providers, and a verb named for it would land there.
* ``overlay=workspaces:templates:<id>`` draws ``WT_TemplateOverlay`` for that template, with the overlay's own chrome (the shell's wrapper is bypassed, so
  there is one title bar, not two).
* The router shim: a bare ``/workspaces/templates`` closes to ``view=platform:templates`` once ``window.WT_TemplatesPage`` is loaded, and opens the
  legacy list overlay before (G1-01, #850). ``templates.jsx`` is what makes the first branch the one that runs.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._mini_react import transpile
from tests.ui._platform_page_harness import platform_page_context
from tests.ui._register_harness import ROOT
from tests.ui.test_shell_dual_render_guard import _all_console_text, _registrations

UI = ROOT / "ui"
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
OVERLAYS = (UI / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
TEMPLATES = UI / "components" / "workspaces" / "templates.jsx"
SHELL_URL = (UI / "foundation" / "shell-url.js").read_text(encoding="utf-8")
SHELL_VERBS = (UI / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")
SHIM = (UI / "foundation" / "shell-router-shim.js").read_text(encoding="utf-8")

_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN:
        _OPEN.pop().close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _registration(verb_id: str) -> dict:
    found = [r for r in _registrations() if r.get("id") == verb_id]
    assert len(found) == 1, verb_id
    return found[0]


def _reg_source(verb_id: str) -> str:
    start = SHELL.index(f'id: "{verb_id}"')
    return SHELL[start:SHELL.index("});", start)]


def test_template_create_is_a_palette_and_overlay_button_verb():
    reg = _registration("template.create")
    assert set(re.findall(r'"([\w-]+)"', reg["surfaces"])) == {"palette", "overlay-button"}
    body = _reg_source("template.create")
    assert 'label: "Create Workspace Template"' in body
    assert 'goView("platform", "templates")' in body and "window.WT_requestCreate" in body


def test_the_custom_page_registers_its_own_open_verb_after_the_platform_loop():
    reg = _registration("platform.open.templates")
    assert set(re.findall(r'"([\w-]+)"', reg["surfaces"])) == {"palette", "platform-nav"}
    assert 'label: "Open Workspace Templates"' in _reg_source("platform.open.templates")
    loop = SHELL.index("window.NV_platVerbSpecs().forEach(")
    gate = SHELL.index('if (window.NV_platNavAvailable("templates")) {')
    end = SHELL.index("}, [registry]);", loop)
    assert loop < gate < SHELL.index('id: "platform.open.templates"') < SHELL.index('id: "template.create"') < end
    block = SHELL[gate:SHELL.index("\n    }\n", gate)]
    assert 'id: "platform.open.templates"' in block and 'id: "template.create"' in block, "both verbs are inside the page's gate"
    assert SHELL.count('id: "template.create"') == 1


TEMPLATES_PAGE_STAND_IN = "window.WT_TemplatesPage = function () { return null; };"


def _shell(page_loaded: bool = True):
    """The shell's core verb effect, as nv-shell.jsx writes it, against the real registry; goView and the page's request are recorded."""
    ctx = platform_page_context(prelude_extra=SHELL_URL + SHELL_VERBS + (TEMPLATES_PAGE_STAND_IN if page_loaded else ""))
    _OPEN.append(ctx)
    start = SHELL.index("// --- Core verbs (chrome-level; view bodies add their own)")
    body = SHELL.index("React.useEffect(function () {", start) + len("React.useEffect(function () {")
    effect = SHELL[body:SHELL.index("}, [registry]);", body)]
    ctx.eval(
        "var registry = SH_createVerbRegistry(); var VIEWS = []; var REQUESTS = 0; window.WT_requestCreate = function () { REQUESTS += 1; };"
        "(function (registry, goView) {" + effect + "\n})(registry, function (n, v, s) { VIEWS.push([n, v || null, s || null]); });"
    )
    return ctx


@pytest.fixture
def shell():
    return _shell()


def test_without_the_page_neither_verb_registers():
    ctx = _shell(page_loaded=False)
    ids = js(ctx, "registry.ids()")
    assert "platform.open.templates" not in ids and "template.create" not in ids
    assert "workspace.create" in ids and "platform.open.providers" in ids, "every other verb still registers"


def test_the_verbs_register_in_the_real_registry_and_open_the_page(shell):
    assert js(shell, "SH_lintVerbLabel('Create Workspace Template')") is None
    assert js(shell, "SH_lintVerbLabel('Open Workspace Templates')") is None
    ids = js(shell, "registry.ids()")
    assert ids[-2:] == ["platform.open.templates", "template.create"], "after the Platform loop, so the core verbs keep the empty query"
    assert not {"platform.open.templates", "template.create"} & set(js(shell, "SH_rankVerbs(registry, '', {}).slice(0, 8).map(function (v) { return v.id; })"))
    shell.eval("registry.get('template.create').run();")
    assert js(shell, "[VIEWS, REQUESTS]") == [[["platform", "templates", None]], 1]
    shell.eval("registry.get('platform.open.templates').run();")
    assert js(shell, "[VIEWS[1], REQUESTS]") == [["platform", "templates", None], 1]


def test_the_palette_ranks_each_label_first(shell):
    for label, verb in (("Create Workspace Template", "template.create"), ("create workspace template", "template.create"),
                        ("Open Workspace Templates", "platform.open.templates"), ("Create Workspace", "workspace.create")):
        assert js(shell, "SH_rankVerbs(registry, " + json.dumps(label) + ", {})[0].id") == verb, label


def test_the_verb_has_its_pointer_affordances():
    assert 'data-verb="template.create"' in _all_console_text(), "the dual-render guard reads the console files"
    assert 'data-verb="template.create"' in TEMPLATES.read_text(encoding="utf-8"), "the page's empty state"
    dialog = OVERLAYS[OVERLAYS.index("function NV_CreateWorkspaceOverlay("):OVERLAYS.index("// Legacy adapter")]
    button = dialog[dialog.index('data-verb="template.create"') - 400:dialog.index('data-verb="template.create"') + 400]
    assert 'con.registry.get("template.create")' in button and "con.closeOverlay()" in button and "Create a template" in button


def test_the_overlay_address_draws_the_template_overlay_without_double_chrome():
    mount = OVERLAYS[OVERLAYS.index("  workspaces: {"):OVERLAYS.index("// The heading each overlay shows above its page.")]
    branch = mount[mount.index('if (state.section === "templates")'):mount.index('if (state.section === "providers")')]
    assert "<window.WT_TemplateOverlay" in branch and "templateId={state.id}" in branch and "onClose={shell.closeOverlay}" in branch
    assert 'name === "workspaces" && overlay.section === "templates" && !!overlay.id' in OVERLAYS


# ---- the router shim's two branches, with the real templates.jsx ------------------------------------------------------------------------------------


def _shim():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN.append(ctx)
    ctx.eval("var window = globalThis;")
    ctx.eval(SHELL_URL)
    ctx.eval(SHIM)
    return ctx


def _navigate(ctx) -> list:
    return js(ctx, "(function () { var calls = [];"
                   " SH_shimNavigate({ name: 'workspaces', section: 'templates', id: 't1' }, function (n, s, i) { calls.push(['open', n, s, i]); },"
                   " '/workspaces/templates', null, function (t) { calls.push(['closeTo', t]); }, function () {}); return calls; })()")


def test_the_real_page_makes_the_bare_list_path_close_to_the_platform_page():
    ctx = _shim()
    assert _navigate(ctx) == [["open", "workspaces", "templates", None]], "before the page is loaded: the legacy list overlay"
    ctx.eval(transpile(TEMPLATES))
    assert ctx.eval("typeof window.WT_TemplatesPage") == "function"
    assert _navigate(ctx) == [["closeTo", {"name": "platform", "nav": "templates", "sub": None}]]
    assert js(ctx, "SH_parseUrl('#/?view=platform:templates').view") == {"name": "platform", "nav": "templates", "sub": None}
    assert js(ctx, "SH_parseUrl('#/?overlay=workspaces:templates:t1').overlay") == {"name": "workspaces", "section": "templates", "id": "t1"}


def test_the_real_page_is_a_platform_nav():
    ctx = platform_page_context(prelude_extra=SHELL_URL)
    _OPEN.append(ctx)
    assert ctx.eval("NV_platNavAvailable('templates')") is False
    ctx.eval(transpile(TEMPLATES))
    assert ctx.eval("NV_platNavAvailable('templates')") is True and ctx.eval("NV_platResolveNav('templates')") == "templates"
    assert ctx.eval("NV_platNavLabel('templates')") == "Workspace templates"
