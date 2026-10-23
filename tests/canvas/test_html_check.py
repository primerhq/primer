"""The Html script check: inline scripts are compiled (never run) and external ones come from four CDNs."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from py_mini_racer import JSTimeoutException
from py_mini_racer import MiniRacer as RealMiniRacer

from primer.canvas.html_check import SCRIPT_HOSTS, check_html_scripts
from primer.canvas.lang import parse_program
from primer.canvas.state import CanvasState, Limits, apply_batch

ALLOWED = "only cdnjs.cloudflare.com, cdn.jsdelivr.net, unpkg.com and esm.sh are allowed"
HIDDEN_ONE_OF_ONE = (
    "1 of the 1 '<script' in the html is not a script the check can read; write each script as a plain <script> element "
    "and split '<script' in any text or string ('<scr' + 'ipt')"
)


def test_the_allowed_hosts():
    assert SCRIPT_HOSTS == ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "esm.sh")


# --- the plan's tests -----------------------------------------------------------------------


def test_a_valid_script_passes():
    assert check_html_scripts("<div id=b></div><script>b.textContent='3 tracks queued'</script>") == []


def test_a_syntax_error_names_the_script_and_v8s_message():
    assert check_html_scripts("<script>ok()</script><script>var b = ;</script>") == ["script 2 does not parse: Unexpected token ';'"]


def test_a_script_that_closes_a_wrapper_is_compiled_not_run():
    assert check_html_scripts("<script>}; globalThis.ran = 1; function x() {</script>")[0].startswith("script 1 does not parse")


@pytest.mark.parametrize("src, problems", [
    ("https://cdn.jsdelivr.net/npm/chart.js", []),
    ("http://unpkg.com/x.js", ["script 1 src must be https"]),
    ("https://evil.example/x.js", [f"script 1 loads from evil.example; {ALLOWED}"]),
])
def test_script_sources(src, problems):
    assert check_html_scripts(f'<script src="{src}"></script>') == problems


def test_module_and_data_scripts_are_skipped():
    assert check_html_scripts("<script type=module>import x from 'y'</script><script type=application/json>{bad</script>") == []


# --- sources, in detail -------------------------------------------------------------------------


@pytest.mark.parametrize("src", [
    "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
    "https://cdn.jsdelivr.net/npm/chart.js@4",
    "https://unpkg.com/react@18/umd/react.production.min.js",
    "https://esm.sh/preact",
    "HTTPS://UNPKG.COM/x.js",
    "https://Unpkg.com:443/x.js",
    "  https://unpkg.com/x.js  ",
    "https://unpkg.com/x.js?a=1&b=2#frag",
])
def test_sources_that_are_allowed(src):
    assert check_html_scripts(f'<script src="{src}"></script>') == []


@pytest.mark.parametrize("src", ["x.js", "/x.js", "//unpkg.com/x.js", "", "data:text/javascript,alert(1)", "javascript:alert(1)", "ftp://unpkg.com/x.js", "https:///x.js", "https:x.js"])
def test_sources_that_are_not_https(src):
    assert check_html_scripts(f'<script src="{src}"></script>') == ["script 1 src must be https"]


@pytest.mark.parametrize("src, shown", [
    ("https://unpkg.com:8443/x.js", "unpkg.com:8443"),
    ("https://unpkg.com@evil.example/x.js", "unpkg.com@evil.example"),
    ("https://evil.example\\@unpkg.com/x.js", "evil.example\\@unpkg.com"),
    ("https://unpkg.com.evil.example/x.js", "unpkg.com.evil.example"),
    ("https://notunpkg.com/x.js", "notunpkg.com"),
    ("https://unpkg.com./x.js", "unpkg.com."),
    ("https://EVIL.example/x.js", "evil.example"),
    ("https://raw.githubusercontent.com/x.js", "raw.githubusercontent.com"),
])
def test_hosts_that_are_not_allowed_are_named_as_the_browser_would_read_them(src, shown):
    assert check_html_scripts(f'<script src="{src}"></script>') == [f"script 1 loads from {shown}; {ALLOWED}"]


def test_a_valueless_src_and_a_repeated_src():
    assert check_html_scripts("<script src></script>") == ["script 1 src must be https"]
    # the browser uses the first src, so that is the one judged
    assert check_html_scripts('<script src="https://unpkg.com/a.js" src="http://evil.example/b.js"></script>') == []
    assert check_html_scripts('<script src="http://evil.example/b.js" src="https://unpkg.com/a.js"></script>') == ["script 1 src must be https"]


def test_a_script_with_a_src_ignores_its_inline_text_as_the_browser_does():
    assert check_html_scripts('<script src="https://unpkg.com/x.js">var = ;</script>') == []


def test_module_scripts_still_need_an_allowed_source():
    assert check_html_scripts('<script type=module src="http://esm.sh/x"></script>') == ["script 1 src must be https"]
    assert check_html_scripts('<script type=module src="https://esm.sh/x"></script>') == []


# --- an SVG script names its url in href or xlink:href ----------------------------------------------------


def _svg_script(attribute: str, url: str) -> str:
    return f'<svg xmlns:xlink="http://www.w3.org/1999/xlink"><script {attribute}="{url}"></script></svg>'


@pytest.mark.parametrize("attribute", ["href", "xlink:href", "HREF", "XLink:Href"])
def test_an_svg_script_loads_from_href_or_xlink_href_so_those_are_judged_like_a_src(attribute):
    name = attribute.lower()
    assert check_html_scripts(_svg_script(attribute, "https://evil.example/x.js")) == [f"script 1 loads from evil.example; {ALLOWED}"]
    assert check_html_scripts(_svg_script(attribute, "http://unpkg.com/x.js")) == [f"script 1 {name} must be https"]
    assert check_html_scripts(_svg_script(attribute, "//unpkg.com/x.js")) == [f"script 1 {name} must be https"]
    assert check_html_scripts(_svg_script(attribute, "https://unpkg.com@evil.example/x.js")) == [
        f"script 1 loads from unpkg.com@evil.example; {ALLOWED}"
    ]
    assert check_html_scripts(_svg_script(attribute, "https://cdn.jsdelivr.net/npm/chart.js")) == []
    assert check_html_scripts(_svg_script(attribute, "  HTTPS://Unpkg.com:443/x.js  ")) == []


def test_the_svg_scripts_the_review_found_were_not_judged():
    loads = f"script 1 loads from evil.example; {ALLOWED}"
    assert check_html_scripts('<svg><script href="https://evil.example/x.js"></script></svg>') == [loads]
    assert check_html_scripts('<svg><script xlink:href="https://evil.example/x.js"></script></svg>') == [loads]
    # SVG allows self-closing, but the check cannot tell an SVG script from an HTML one, where the slash is ignored and what follows
    # runs: the URL is still judged, and the tag fails closed (review N1 on #856).
    assert check_html_scripts('<svg><script href="https://evil.example/x.js"/></svg>') == [loads, HIDDEN_ONE_OF_ONE]


def test_every_url_attribute_a_script_has_is_judged_so_a_good_one_never_excuses_a_bad_one():
    # the check does not know whether a script is HTML (it loads src) or SVG (href, else xlink:href)
    good, bad = "https://unpkg.com/a.js", "https://evil.example/b.js"
    loads = f"script 1 loads from evil.example; {ALLOWED}"
    assert check_html_scripts(f'<script src="{good}" href="{bad}"></script>') == [loads]
    assert check_html_scripts(f'<svg><script href="{good}" xlink:href="{bad}"></script></svg>') == [loads]
    assert check_html_scripts(f'<svg><script href="{bad}" xlink:href="http://unpkg.com/c.js"></script></svg>') == [loads, "script 1 xlink:href must be https"]
    assert check_html_scripts(f'<svg><script href="{good}" xlink:href="{good}"></script></svg>') == []
    assert check_html_scripts(f'<script src="{good}"></script><svg><script href="{bad}"></script></svg>') == [f"script 2 loads from evil.example; {ALLOWED}"]


def test_a_valueless_or_empty_href_is_not_https_and_an_svg_script_without_a_url_is_compiled():
    assert check_html_scripts("<svg><script href></script></svg>") == ["script 1 href must be https"]
    assert check_html_scripts('<svg><script xlink:href=""></script></svg>') == ["script 1 xlink:href must be https"]
    assert check_html_scripts("<svg><script>var = ;</script></svg>") == ["script 1 does not parse: Unexpected token '='"]
    assert check_html_scripts("<svg><script>ok()</script></svg>") == []


# --- which scripts are compiled -------------------------------------------------------------------


@pytest.mark.parametrize("type_attr", ["", " type", " type=''", " type=text/javascript", " type=application/javascript",
                                       " type='TEXT/JavaScript'", " type='text/javascript; charset=utf-8'", " type=text/ecmascript", " type=application/x-javascript"])
def test_classic_scripts_are_compiled(type_attr):
    assert check_html_scripts(f"<script{type_attr}>var b = ;</script>") == ["script 1 does not parse: Unexpected token ';'"]


@pytest.mark.parametrize("type_attr", ["module", "importmap", "speculationrules", "application/json", "text/plain", "text/babel", "text/x-template", "application/ld+json"])
def test_every_other_type_is_skipped(type_attr):
    assert check_html_scripts(f"<script type='{type_attr}'>var b = ;</script>") == []


def test_scripts_are_numbered_in_document_order_counting_every_script():
    html = (
        '<script src="https://unpkg.com/a.js"></script>'
        "<script type=module>import x from 'y'</script>"
        "<script>ok()</script>"
        "<script>var = 1;</script>"
        '<script src="http://evil.example/z.js"></script>'
    )
    assert check_html_scripts(html) == ["script 4 does not parse: Unexpected token '='", "script 5 src must be https"]


def test_the_problems_of_one_document_are_listed_in_order():
    html = '<script src="http://unpkg.com/a.js"></script><script>var = ;</script><script src="https://evil.example/b.js"></script>'
    assert check_html_scripts(html) == [
        "script 1 src must be https",
        "script 2 does not parse: Unexpected token '='",
        f"script 3 loads from evil.example; {ALLOWED}",
    ]


@pytest.mark.parametrize("html, count", [
    ("<SCRIPT>var = ;</SCRIPT>", 1),
    ("<script>var = ;", 1),                                     # never closed: still checked (a browser does not run it)
    ("<script>ok()</script ><script>var = ;</script>", 1),
    ("<div><p>text</p><script>var = ;</script></div>", 1),
])
def test_scripts_are_found_the_way_a_browser_finds_them(html, count):
    assert len(check_html_scripts(html)) == count


@pytest.mark.parametrize("html", [
    "<p>no scripts here</p>",
    "",
    "<script></script>",
    "<script>   \n  </script>",
    "<style>.a { color: red }</style>",
    "<script>var s = '</scr' + 'ipt>';</script>",
    "<script>if (a<b && c>d) { x(); }</script>",
    '<script>var s = "a\\"b\\\\n\u2028 café \U0001F600";</script>',
    "<script>// a comment\nvar x = 1; /* another */</script>",
    "<script>async function f() { await g(); }</script>",
    "<script>class A { #p = 1; static s() {} }</script>",
])
def test_things_that_are_not_problems(html):
    assert check_html_scripts(html) == []


@pytest.mark.parametrize("source, message", [
    ("var b = ;", "Unexpected token ';'"),
    ("await x", "await is only valid in async functions and the top level bodies of modules"),
    ("import x from 'y'", "Cannot use import statement outside a module"),
    ("let a = 1; let a = 2;", "Identifier 'a' has already been declared"),
    ("(function(){", "Unexpected end of input"),
    ("'use strict'; with (a) {}", "Strict mode code may not include a with statement"),
])
def test_v8_says_what_is_wrong(source, message):
    assert check_html_scripts(f"<script>{source}</script>") == [f"script 1 does not parse: {message}"]


# --- never run, bounded, and cleaned up -----------------------------------------------------------


def test_a_script_is_compiled_and_never_run():
    started = time.perf_counter()
    assert check_html_scripts("<script>while (true) {}</script>") == []
    assert check_html_scripts("<script>throw new Error('boom')</script>") == []
    assert time.perf_counter() - started < 2


def test_an_enormous_or_hostile_script_is_a_problem_not_a_hang():
    started = time.perf_counter()
    (deep,) = check_html_scripts("<script>x = " + "(" * 50000 + "1" + ")" * 50000 + "</script>")
    assert deep == "script 1 does not parse: Maximum call stack size exceeded"
    (long_name,) = check_html_scripts("<script>var " + "a" * 5000 + " = ;</script>")
    assert long_name == "script 1 does not parse: Unexpected token ';'"
    assert check_html_scripts("<script>" + "var a = 0;\n" * 5000 + "</script>") == []
    assert time.perf_counter() - started < 5


def test_a_long_engine_message_is_cut_short():
    (problem,) = check_html_scripts("<script>x = 1 " + "a" * 3000 + "</script>")
    assert problem.startswith("script 1 does not parse: Unexpected identifier 'aaaa")
    assert len(problem) == len("script 1 does not parse: ") + 200
    assert problem.endswith("...")


class _Spy:
    """Stands in for MiniRacer, wrapping the real one so the check still works."""

    made: list["_Spy"] = []

    def __init__(self):
        self.ctx = RealMiniRacer()
        self.thread = threading.get_ident()
        self.closed = False
        self.evals: list[tuple[str, float | None]] = []
        _Spy.made.append(self)

    def set_hard_memory_limit(self, limit):
        self.ctx.set_hard_memory_limit(limit)

    def eval(self, code, timeout_sec=None):
        self.evals.append((code, timeout_sec))
        return self.ctx.eval(code, timeout_sec=timeout_sec)

    def close(self):
        self.closed = True
        self.ctx.close()


@pytest.fixture()
def spy(monkeypatch):
    import py_mini_racer

    _Spy.made = []
    monkeypatch.setattr(py_mini_racer, "MiniRacer", _Spy)
    return _Spy


def test_no_engine_is_started_when_there_is_nothing_to_compile(spy):
    check_html_scripts("<p>nothing</p>")
    check_html_scripts('<script src="https://unpkg.com/x.js"></script><script type=module>x</script>')
    check_html_scripts('<script src="http://evil.example/x.js"></script>')
    assert spy.made == []


def test_one_engine_serves_every_script_of_a_document_and_is_closed(spy):
    assert check_html_scripts("<script>a()</script><script>var = ;</script><script>b()</script>") == ["script 2 does not parse: Unexpected token '='"]
    (engine,) = spy.made
    assert engine.closed and len(engine.evals) == 3
    assert all(timeout == 1.0 for _, timeout in engine.evals)


def test_the_source_travels_as_data_inside_a_fixed_probe(spy):
    check_html_scripts("<script>x = \"a\\\\b\" + '\u2028';</script>")
    (engine,) = spy.made
    ((code, _),) = engine.evals
    assert code.startswith("(function (s) { try { new Function(s); return null; } catch (e) { return String(e.message); } })(")
    assert code.endswith(")") and "\u2028" not in code                  # the line separator is escaped, so it cannot end the literal


def test_a_top_level_return_is_the_known_blind_spot():
    # new Function() accepts what a classic script refuses; the browser reports it at run time.
    # The module says so. Wrapping the source to catch it would let a crafted script reach the probe.
    assert check_html_scripts("<script>return 1</script>") == []


# --- safe wherever it is called, and closed on every failure -------------------------------------------------

THREE_SCRIPTS = '<script>a()</script><script src="http://evil.example/x.js"></script><script>b()</script>'
UNCHECKED_AROUND_ONE_BAD_SOURCE = [
    "script 1 could not be checked: the syntax check failed",
    "script 2 src must be https",
    "script 3 could not be checked: the syntax check failed",
]


async def test_it_works_on_a_thread_that_has_a_running_event_loop():
    # mini-racer adopts the running loop of the thread that makes an engine and then refuses a timeout: an AssertionError
    assert check_html_scripts("<script>ok()</script><script>var = ;</script>") == ["script 2 does not parse: Unexpected token '='"]
    assert check_html_scripts("<script>ok()</script>") == []


def test_it_works_under_asyncio_run():
    async def main():
        return check_html_scripts("<script>var = ;</script>")

    assert asyncio.run(main()) == ["script 1 does not parse: Unexpected token '='"]


async def test_apply_batch_with_the_check_works_inside_a_running_loop():
    refused = apply_batch(
        CanvasState.empty(), parse_program('root = Column([v])\nv = Html("<script>var = ;</script>")'), limits=Limits(), script_check=check_html_scripts
    )
    assert refused.state is None
    assert [e.render() for e in refused.errors] == ["line 2: v: script 1 does not parse: Unexpected token '='"]
    fine = apply_batch(
        CanvasState.empty(), parse_program('root = Column([v])\nv = Html("<script>ok()</script>")'), limits=Limits(), script_check=check_html_scripts
    )
    assert fine.errors == () and fine.state is not None


def test_with_no_loop_running_the_engine_lives_on_the_callers_thread(spy):
    check_html_scripts("<script>a()</script>")
    (engine,) = spy.made
    assert engine.thread == threading.get_ident()


async def test_with_a_loop_running_the_engine_lives_on_a_worker_thread_that_is_gone_afterwards(spy):
    assert check_html_scripts("<script>a()</script><script>b()</script>") == []
    (engine,) = spy.made
    assert engine.thread != threading.get_ident() and engine.closed and len(engine.evals) == 2
    assert not [t for t in threading.enumerate() if t.name.startswith("canvas-script-check")]


@pytest.mark.parametrize("failure", [JSTimeoutException(), AssertionError("To apply a timeout in an async context"), RuntimeError("boom"), ValueError("bad"), MemoryError()],
                         ids=lambda failure: type(failure).__name__)
def test_any_failure_of_the_engine_is_a_problem_never_an_exception(spy, monkeypatch, failure):
    def boom(self, code, **kwargs):
        raise failure

    monkeypatch.setattr(_Spy, "eval", boom)
    assert check_html_scripts(THREE_SCRIPTS) == UNCHECKED_AROUND_ONE_BAD_SOURCE
    assert spy.made[0].closed


def test_an_engine_that_cannot_start_is_a_problem_for_each_inline_script(monkeypatch):
    import py_mini_racer

    class Broken:
        def __init__(self):
            raise RuntimeError("no engine here")

    monkeypatch.setattr(py_mini_racer, "MiniRacer", Broken)
    assert check_html_scripts(THREE_SCRIPTS) == UNCHECKED_AROUND_ONE_BAD_SOURCE


def test_an_engine_whose_memory_limit_cannot_be_set_is_closed_and_a_problem(spy, monkeypatch):
    def refuse(self, limit):
        raise RuntimeError("no limit")

    monkeypatch.setattr(_Spy, "set_hard_memory_limit", refuse)
    assert check_html_scripts("<script>a()</script>") == ["script 1 could not be checked: the syntax check failed"]
    assert spy.made[0].closed


def test_a_close_that_fails_changes_no_answer(spy, monkeypatch):
    def refuse(self):
        self.ctx.close()
        raise RuntimeError("close failed")

    monkeypatch.setattr(_Spy, "close", refuse)
    assert check_html_scripts("<script>var = ;</script>") == ["script 1 does not parse: Unexpected token '='"]


async def test_a_worker_thread_that_cannot_be_started_is_a_problem_not_an_exception(monkeypatch):
    class NoThreads:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, *args, **kwargs):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr("primer.canvas.html_check.ThreadPoolExecutor", NoThreads)
    assert check_html_scripts(THREE_SCRIPTS) == UNCHECKED_AROUND_ONE_BAD_SOURCE


# --- parser differentials (audit G8-06): what a browser runs that html.parser alone would not see --------------
#
# Each shape is HTML a Chromium srcdoc frame (scripting enabled) parses into a script it RUNS or FETCHES (the audit's browser probe),
# while html.parser with scripting off and no foreign-content rules sees no script. The check now parses with scripting on, and
# refuses a body in which the raw '<script' count (any case) is more than the scripts the parser found: every shape is refused or
# reported. EVIL is not a CDN, so a script the parser does see is refused for its host; BAD is inline code that does not parse.

EVIL = "https://evil.example/x.js"
BAD = "var = ;"
HIDDEN = "is not a script the check can read"

NOSCRIPT_EXT = f'<noscript><p title="</noscript><script src={EVIL}></script>"></noscript>'
NOSCRIPT_BAD = f'<noscript><p title="</noscript><script>{BAD}</script>"></noscript>'
SVG_STYLE_EXT = f'<svg><style><script href="{EVIL}"></script></style></svg>'
SVG_STYLE_BAD = f"<svg><style><script>{BAD}</script></style></svg>"
SVG_IFRAME_BAD = f"<svg><iframe><script>{BAD}</script></iframe></svg>"
SVG_TEXTAREA_EXT = f'<svg><textarea><script href="{EVIL}"></script></textarea></svg>'
SVG_XMP_BAD = f"<svg><xmp><script>{BAD}</script></xmp></svg>"
SVG_TITLE_EXT = f'<svg><title><script src="{EVIL}"></script></title></svg>'
DOUBLE_ESCAPED_BAD = f"<script><!--<script></script>\n{BAD}</script>"


def test_noscript_is_parsed_as_a_browser_with_scripting_on_parses_it():
    """With scripting on, <noscript> is raw text: the </noscript> inside the attribute ends it and the script after it is real."""
    assert check_html_scripts(NOSCRIPT_EXT) == [f"script 1 loads from evil.example; {ALLOWED}"]
    assert check_html_scripts(NOSCRIPT_BAD)[0].startswith("script 1 does not parse")


@pytest.mark.parametrize("html", [
    NOSCRIPT_EXT, NOSCRIPT_BAD, SVG_STYLE_EXT, SVG_STYLE_BAD, SVG_IFRAME_BAD, SVG_TEXTAREA_EXT, SVG_XMP_BAD, SVG_TITLE_EXT,
    DOUBLE_ESCAPED_BAD,
], ids=[
    "noscript-ext", "noscript-bad", "svg-style-ext", "svg-style-bad", "svg-iframe-bad", "svg-textarea-ext", "svg-xmp-bad",
    "svg-title-ext", "double-escaped-bad",
])
def test_the_nine_shapes_a_browser_runs_are_refused_or_reported(html):
    assert check_html_scripts(html) != []


@pytest.mark.parametrize("html", [
    SVG_STYLE_EXT, SVG_STYLE_BAD, SVG_IFRAME_BAD, SVG_TEXTAREA_EXT, SVG_XMP_BAD, SVG_TITLE_EXT, DOUBLE_ESCAPED_BAD,
    f'<!--><script src="{EVIL}"></script>-->',
    f'<!---><script src="{EVIL}"></script>-->',
    f'<![CDATA[ x > <script src="{EVIL}"></script> ]]>',
], ids=[
    "svg-style-ext", "svg-style-bad", "svg-iframe-bad", "svg-textarea-ext", "svg-xmp-bad", "svg-title-ext", "double-escaped-bad",
    "<!-->", "<!--->", "<![CDATA[ x >",
])
def test_a_script_tag_the_parser_cannot_read_fails_closed(html):
    problems = check_html_scripts(html)
    assert problems and HIDDEN in problems[-1], problems


def test_the_hidden_script_problem_counts_every_script_tag_in_any_case():
    assert check_html_scripts(f'<script>ok()</script><SVG><STYLE><ScRiPt href="{EVIL}"></ScRiPt></STYLE></SVG>') == [
        "1 of the 2 '<script' in the html is not a script the check can read; write each script as a plain <script> element "
        "and split '<script' in any text or string ('<scr' + 'ipt')"
    ]
    assert check_html_scripts("<svg><style><script></script><script></script></style></svg>") == [
        "2 of the 2 '<script' in the html are not a script the check can read; write each script as a plain <script> element "
        "and split '<script' in any text or string ('<scr' + 'ipt')"
    ]


@pytest.mark.parametrize("html", [
    f"<script/>{BAD}</script>",
    f"<SCRIPT />{BAD}</SCRIPT>",
    f'<script src="{EVIL}"/>',
    '<svg><script href="https://unpkg.com/x.js"/></svg>',
], ids=["self-closed then bad code", "upper case", "self-closed with a src", "self-closed svg script"])
def test_a_self_closed_script_tag_fails_closed(html):
    """Review N1 on #856: a browser ignores the slash of ``<script/>`` in HTML and runs what follows; html.parser read it as an empty
    script, so the count matched and ``<script/>bad(</script>`` passed. It is not counted as a script found now."""
    problems = check_html_scripts(html)
    assert problems and HIDDEN in problems[-1], problems


@pytest.mark.parametrize("html", [
    f'<p title="<script src={EVIL}></script>">',
    "<!-- <script>old()</script> -->",
    "<script>const tag = '<script>';</script>",
], ids=["in an attribute", "in a comment", "in a string inside a script"])
def test_fail_closed_also_refuses_script_text_a_browser_would_not_run(html):
    """The count cannot tell text from a hidden tag, so it refuses both: the agent splits the word instead."""
    assert HIDDEN in check_html_scripts(html)[-1]


def test_well_formed_scripts_are_not_counted_as_hidden():
    assert check_html_scripts("<script>a()</script>\n<SCRIPT src='https://unpkg.com/x.js'></SCRIPT><p>scripts</p>") == []


def test_a_hidden_script_is_an_error_of_its_html_element():
    r = apply_batch(CanvasState.empty(), parse_program(f'root = Column([v])\nv = Html("""{SVG_TITLE_EXT}""")'), limits=Limits(),
                    script_check=check_html_scripts)
    assert r.state is None and len(r.errors) == 1 and r.errors[0].line == 2 and r.errors[0].message.startswith("v: 1 of the 1 '<script'")


# --- known limits: what the check cannot see. The frame's CSP (Task 13) is the only control in the browser. -----


@pytest.mark.parametrize("html", [
    f'<img src=x onerror="fetch(\'{EVIL}\')">',
    '<a href="#" onclick="var = ;">go</a>',
], ids=["a handler that fetches", "a handler that does not parse"])
def test_known_limit_event_handler_attributes_are_not_checked(html):
    assert check_html_scripts(html) == []


@pytest.mark.parametrize("html", ['<a href="javascript:alert(1)">go</a>', '<iframe src="javascript:alert(1)"></iframe>'])
def test_known_limit_javascript_urls_are_not_checked(html):
    assert check_html_scripts(html) == []


@pytest.mark.parametrize("html", [
    f'<script type="module">import "{EVIL}"</script>',
    f'<script type="module">const m = await import("{EVIL}")</script>',
    f'<script>import("{EVIL}")</script>',
], ids=["import in a module", "import() in a module", "import() in a classic script"])
def test_known_limit_import_is_not_checked(html):
    assert check_html_scripts(html) == []


def test_known_limit_a_script_inside_a_nested_srcdoc_frame_is_not_seen():
    assert check_html_scripts(f'<iframe srcdoc="&lt;script src=&quot;{EVIL}&quot;&gt;&lt;/script&gt;"></iframe>') == []
    assert check_html_scripts('<iframe srcdoc="&lt;script&gt;var = ;&lt;/script&gt;"></iframe>') == []
