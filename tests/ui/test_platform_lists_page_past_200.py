"""Every Platform page reads its whole list (ticket 01a16dfa): the ``list`` of each ``NV_PLAT_PAGES`` entry, its poll and its note.

Each entry read ``/<plural>?limit=200`` and stopped, so the 201st agent, graph, collection, trigger, channel, harness, service, approval policy or LLM profile
had no card. Each reads every page now with the foundation's ``PR_fetchAllPages`` (``ui/foundation/api.js``); the page polls every 15 s PER PAGE READ
(``PR_usePagedResource``: ruling 4, a list of three pages costs one request per 15 s on average, as the one page it replaced) and says a list cut short
(``PR_PagedNote``, ``nv-plat-note``). The page runs on ``tests/ui/_platform_page_harness.py`` (the real kit, ``nv-platform.jsx`` and ``api.js``).
"""

from __future__ import annotations

import json

import pytest

from tests.ui._platform_page_harness import platform_page_context
from tests.ui.test_platform_list_page import QUIET_AUDIT

# The harness answers every resource {items: ITEMS}; EXTRA is laid over that answer (the pages the read took, a cut-short list).
_EXTRA = """
var EXTRA = null;
(function () {
  var inner = window.primerApi.useResource;
  window.primerApi.useResource = function (key, fn, opts) {
    var res = inner(key, fn, opts);
    if (EXTRA && res.data) res.data = Object.assign({}, res.data, EXTRA);
    return res;
  };
})();
"""

# Every list entry and the path it reads; the server pages them all the same way.
LISTS = [
    ("agents", "/agents"), ("graphs", "/graphs"), ("collections", "/collections"), ("triggers", "/triggers"), ("channels", "/channels"),
    ("harnesses", "/harnesses"), ("services", "/services"), ("approvals", "/tool_approval_policies"), ("profiles", "/model_profiles"),
]


@pytest.fixture
def ctx():
    c = platform_page_context(prelude_extra=QUIET_AUDIT + _EXTRA)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.mark.parametrize("nav,path", LISTS)
def test_each_page_reads_every_page_of_its_list(ctx, nav, path):
    ctx.eval(
        "var URLS = [], OUT = null;"
        "function fake(method, url) { URLS.push(url); var m = /[?&]limit=(\\d+)&offset=(\\d+)/.exec(url); var limit = Number(m[1]), offset = Number(m[2]);"
        "  var n = Math.max(0, Math.min(limit, 450 - offset)), items = [];"
        "  for (var i = 0; i < n; i++) items.push({ id: 'r-' + (offset + i), members: [] });"
        "  return Promise.resolve({ kind: 'offset', offset: offset, length: n, total: 450, items: items }); }"
        f"NV_PLAT_PAGES[{json.dumps(nav)}].list(fake, null).then(function (r) {{ OUT = r; }});"
    )
    assert js(ctx, "URLS") == [f"{path}?limit=200&offset=0", f"{path}?limit=200&offset=200", f"{path}?limit=200&offset=400"]
    out = js(ctx, "OUT")
    assert len(out["items"]) == 450 and out["items"][-1]["id"] == "r-449" and out["truncated"] is False


def test_a_page_of_three_pages_polls_every_45_s(ctx):
    ctx.eval("ITEMS = [{ id: 'a0' }]; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    assert js(ctx, "RESOURCE_OPTS['nv-plat:agents'].pollMs") == 15000
    ctx.eval("EXTRA = { pages: 3 }; MR.rerender();")
    assert js(ctx, "RESOURCE_OPTS['nv-plat:agents'].pollMs") == 45000


def test_a_page_that_does_not_poll_still_does_not(ctx):
    ctx.eval("ITEMS = [{ id: 'web__search' }]; EXTRA = { pages: 3 }; MR.mount(NV_PlatListPage, { nav: 'tools' });")
    assert js(ctx, "RESOURCE_OPTS['nv-plat:tools'].pollMs") == 0


def test_a_list_cut_short_says_so_under_the_header(ctx):
    ctx.eval("ITEMS = [{ id: 'a0' }]; MR.mount(NV_PlatListPage, { nav: 'agents' });")
    assert js(ctx, "MR.find('nv-plat-note')") is None
    ctx.eval("EXTRA = { truncated: true, serverTotal: 12000, error: null }; MR.rerender();")
    assert js(ctx, "MR.find('nv-plat-note').props.children") == "Showing the first 1 of 12000 agents."
