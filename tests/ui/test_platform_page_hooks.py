"""The Platform page calls its hooks the same way on every render (hook-order ticket of the 2026-10-08 admin review).

``NV_PlatPage`` returned the Providers page BEFORE it called its own hooks (``if (nav === "providers") return <NV_ProvidersPlatPage />;`` and then ``useState``, ``useEffect``
and ``useResource``), so ONE component instance rendered with no hooks on Providers and with a dozen on every other page. React does not report that going either way (a
render with no hooks at all uses the mount path, and trips no "fewer hooks" check), but the hooks of the page left behind are never run again, so their cleanups never run:
the list resource of the page the operator just left kept polling every 15 s for as long as Providers stayed open (``tests/ui_e2e/test_platform_nav_switch_journey.py``
measures it: a ``GET /v1/agents?limit=200`` 13.4 s after leaving the Agents page for Providers, none after leaving it for Graphs).

The fix gives the hook-calling page its own component, keyed on the nav, so each page is a separate instance that mounts and unmounts whole and ``NV_PlatPage`` itself only
reads the console and chooses. This file pins that shape in source (this checkout has no render harness) with a rule that would have caught the original: no component in the
file may have a top-level ``return`` before one of its hook calls. The scanner is checked against a synthetic good and a synthetic bad component so a change of its pattern cannot
quietly turn it vacuous.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAT = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")

_HOOK = re.compile(r"\b(?:React\.use[A-Z]\w*|window\.primerApi\.useResource|NV_use[A-Z]\w*)\(")
_TOP_RETURN = re.compile(r"^  (?:if \([^\n]*\) )?return\b", re.M)
_COMPONENT = re.compile(r"^function ([A-Z]\w*)\([^)]*\) \{\n", re.M)


def _returns_before_a_hook(source: str) -> list[str]:
    """Names of the components in ``source`` that have a top-level (two-space indent) ``return`` followed, on a LATER line, by a hook call."""
    bad: list[str] = []
    for m in _COMPONENT.finditer(source):
        end = source.find("\n}\n", m.end())
        if end < 0:
            continue
        body = source[m.end():end + 1]
        hooks = [h.start() for h in _HOOK.finditer(body)]
        for r in _TOP_RETURN.finditer(body):
            line_end = body.find("\n", r.start())
            if any(h > line_end for h in hooks):
                bad.append(m.group(1))
                break
    return bad


def test_the_scanner_flags_a_return_before_a_hook_and_passes_one_after() -> None:
    bad = "function A() {\n  var x = NV_useConsole();\n  if (x.nav === 'p') return null;\n  var s = React.useState(0);\n  return s;\n}\n"
    good = "function B() {\n  var x = NV_useConsole();\n  var s = React.useState(0);\n  if (x.nav === 'p') return null;\n  return s;\n}\n"
    hook_is_the_return = "function useThing() {\n  var x = 1;\n  return React.useMemo(function () { return x; }, [x]);\n}\n"

    assert _returns_before_a_hook(bad) == ["A"]
    assert _returns_before_a_hook(good) == []
    assert _returns_before_a_hook(hook_is_the_return) == []


def test_no_component_of_the_platform_file_returns_before_one_of_its_hooks() -> None:
    assert _returns_before_a_hook(PLAT) == []


def _body(name: str) -> str:
    start = PLAT.index(f"function {name}(")
    return PLAT[start:PLAT.index("\n}\n", start) + 3]


def test_the_page_that_chooses_calls_no_hook_of_its_own_beyond_reading_the_console() -> None:
    body = _body("NV_PlatPage")

    assert "React.use" not in body and "useResource" not in body, "the chooser must not own the list page's state, effects or resource"
    assert "var con = NV_useConsole();" in body
    assert "return <NV_ProvidersPlatPage />;" in body


def test_each_nav_gets_its_own_instance_of_the_page_that_has_the_hooks() -> None:
    body = _body("NV_PlatPage")

    assert '<NV_PlatListPage key={nav} nav={nav} />' in body, "keyed on nav, so a page unmounts whole and its cleanups run"
    list_page = _body("NV_PlatListPage")
    assert "var nav = props.nav;" in list_page
    assert 'con.view.nav' not in list_page, "the list page takes its nav from its key's prop, not from a second read of the console"


def test_the_list_page_stays_between_the_chooser_and_the_platform_shell() -> None:
    """Other tests slice the source from ``function NV_PlatPage(`` to ``function NV_Platform(`` to look at the page's hooks and mounts."""
    assert PLAT.index("function NV_PlatPage(") < PLAT.index("function NV_PlatListPage(") < PLAT.index("function NV_Platform(")


def _without_user_handlers(source: str) -> str:
    """``source`` with every ``onClick={...}`` expression removed (braces matched), so what is left is the render body and the effects: the places where a reset would
    happen WITHOUT the user asking for it. A click handler is the user asking (the Clear filter button of an empty result, #524)."""
    out: list[str] = []
    i = 0
    while True:
        j = source.find("onClick={", i)
        if j < 0:
            out.append(source[i:])
            return "".join(out)
        out.append(source[i:j])
        depth, k = 0, j + len("onClick=")
        while k < len(source):
            depth += {"{": 1, "}": -1}.get(source[k], 0)
            k += 1
            if depth == 0:
                break
        i = k


def test_the_handler_stripper_removes_a_click_handler_and_keeps_an_effect_and_the_render_body() -> None:
    clicked = '<button onClick={function () { setQ(""); setPageNo(0); }}>Clear</button>'
    in_effect = 'React.useEffect(function () { setQ(""); }, [nav]);'
    in_render = 'var x = 1; setQ("");'

    assert 'setQ("")' not in _without_user_handlers(clicked)
    assert 'setQ("")' in _without_user_handlers(in_effect + clicked)
    assert 'setQ("")' in _without_user_handlers(in_render + clicked)
    assert 'setQ("")' not in _without_user_handlers('<a onClick={function () { f({ a: 1 }); setQ(""); }}>x</a>'), "nested braces are matched"
    assert 'setQ("")' in _without_user_handlers(clicked + in_effect), "an effect after a click handler is kept"
    assert 'setQ("")' in _without_user_handlers(clicked + in_render), "render code after a click handler is kept"
    assert 'setQ("")' in _without_user_handlers(clicked + in_effect + clicked), "an effect between two click handlers is kept"


def test_a_section_switch_resets_the_filter_the_page_and_the_form_by_remounting_not_by_an_effect() -> None:
    """``key={nav}`` makes every section a fresh instance, so the effect that used to reset ``q``, ``pageNo`` and ``modal`` when ``nav`` changed can never see a change: it is dead
    code that also hides whether the key still works (the lead's review of #547). The behaviour is pinned by ``test_a_filter_typed_on_one_section_is_empty_on_the_next`` in the nav-switch
    journey, which is only meaningful with the effect gone."""
    list_page = _body("NV_PlatListPage")

    assert not re.search(r'React\.useEffect\(function \(\) \{\s*setQ\(""\);\s*setPageNo\(0\);\s*setModal\(null\);\s*\}, \[nav\]\);', list_page), "the reset effect is dead behind key={nav}"
    assert 'setQ("")' not in _without_user_handlers(list_page), "nothing resets the filter AUTOMATICALLY either: a fresh instance starts empty (a user's Clear filter click may)"
    assert re.search(r"React\.useEffect\(function \(\) \{ setPageNo\(0\); \}, \[q\]\);", list_page), "typing a filter still sends the page back to the first one"


def test_the_polling_interval_the_leak_journey_waits_out_is_the_one_the_page_uses() -> None:
    """The journey measures one poll interval (``POLL_SECONDS``) and passes only if no further fetch happens inside it. If the page's interval changed and the constant did not, the journey
    could pass without testing anything."""
    journey = (ROOT / "tests" / "ui_e2e" / "test_platform_nav_switch_journey.py").read_text(encoding="utf-8")
    seconds = re.search(r"^POLL_SECONDS = (\d+)$", journey, re.M)
    assert seconds, "the journey's constant is gone"

    poll = re.search(r"pollMs: (\d+), deps: \[nav\]", _body("NV_PlatListPage"))
    assert poll, "the list page's useResource options changed shape"
    assert int(poll.group(1)) == int(seconds.group(1)) * 1000
