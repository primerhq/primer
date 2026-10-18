"""The Html script check: inline scripts are compiled (never run) and external ones come from four CDNs."""

from __future__ import annotations

import time

import pytest

from py_mini_racer import MiniRacer as RealMiniRacer

from primer.canvas.html_check import SCRIPT_HOSTS, check_html_scripts

ALLOWED = "only cdnjs.cloudflare.com, cdn.jsdelivr.net, unpkg.com and esm.sh are allowed"


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
    assert check_html_scripts('<svg><script href="https://evil.example/x.js"/></svg>') == [loads]        # SVG allows self-closing


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
    ("<script>var = ;", 1),                                     # never closed: the browser runs it to the end
    ("<script>ok()</script ><script>var = ;</script>", 1),
    ("<div><p>text</p><script>var = ;</script></div>", 1),
])
def test_scripts_are_found_the_way_a_browser_finds_them(html, count):
    assert len(check_html_scripts(html)) == count


@pytest.mark.parametrize("html", [
    "<!-- <script>var = ;</script> -->",
    "<p>no scripts here</p>",
    "",
    "<script></script>",
    "<script>   \n  </script>",
    "<style>.a { color: red }</style>",
    "<script>var s = '</scr' + 'ipt>';</script>",
    "<script>if (a<b && c>d) { x(); }</script>",
    '<script>var s = "a\\"b\\\\n  café \U0001F600";</script>',
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
        self.closed = False
        self.evals: list[tuple[str, int | None]] = []
        _Spy.made.append(self)

    def set_hard_memory_limit(self, limit):
        self.ctx.set_hard_memory_limit(limit)

    def eval(self, code, timeout=None):
        self.evals.append((code, timeout))
        return self.ctx.eval(code, timeout=timeout)

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
    assert all(timeout == 1000 for _, timeout in engine.evals)


def test_the_source_travels_as_data_inside_a_fixed_probe(spy):
    check_html_scripts("<script>x = \"a\\\\b\" + ' ';</script>")
    (engine,) = spy.made
    ((code, _),) = engine.evals
    assert code.startswith("(function (s) { try { new Function(s); return null; } catch (e) { return String(e.message); } })(")
    assert code.endswith(")") and " " not in code                  # the line separator is escaped, so it cannot end the literal


def test_the_engine_is_closed_even_when_it_fails_and_a_failure_is_a_problem(spy, monkeypatch):
    from py_mini_racer import JSTimeoutException

    def boom(self, code, timeout=None):
        raise JSTimeoutException()

    monkeypatch.setattr(_Spy, "eval", boom)
    assert check_html_scripts("<script>a()</script><script>b()</script>") == [
        "script 1 could not be checked: the syntax check failed",
        "script 2 could not be checked: the syntax check failed",
    ]
    assert spy.made[0].closed


def test_a_top_level_return_is_the_known_blind_spot():
    # new Function() accepts what a classic script refuses; the browser reports it at run time.
    # The module says so. Wrapping the source to catch it would let a crafted script reach the probe.
    assert check_html_scripts("<script>return 1</script>") == []
