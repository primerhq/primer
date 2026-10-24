"""The Sessions page and a toolset's Sessions tab read every page NEWEST first (#923, round 2, finding 4).

Both read every session (``PR_fetchAllPages``, at most 50 pages: 10000 rows). Session ids are random, so in the server's default order (by id) the cap kept
an arbitrary set of sessions, not the newest. They now ask ``order_by=created_at:desc``, so the cap keeps the newest. The REAL components run on the hook
runtime after the REAL ``api.js``; ``useResource`` records each fetcher, and the fetcher runs against a fake server holding 10050 sessions with random ids
that orders the way ``GET /v1/sessions`` does (by id unless ``order_by`` says otherwise).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")

_PRELUDE = API + r"""
var FETCHERS = {};
window.primerApi.useResource = function (key, fetcher) {
  FETCHERS[key] = fetcher;
  return { data: undefined, loading: true, error: null, degraded: false, refetch: function () {} };
};
window.primerApi.useRouter = function () { return { params: {}, query: {}, navigate: function () {} }; };
window.primerApi.useViewport = function () { return { isMobile: false }; };
var document = { title: "", addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.addEventListener = function () {}; window.removeEventListener = function () {};
["Icon", "StatusPill", "Btn", "Modal", "CardList", "Card", "Fab", "Banner", "FormField", "CapabilityBadges"].forEach(function (n) {
  globalThis[n] = function () { return null; };
});
globalThis.relativeTime = function (s) { return String(s); };
window.describeSessionState = function () { return {}; };
// 10050 sessions; index 0 is the newest. Ids are random-looking, so id order says nothing about age.
var SESSIONS = [];
for (var i = 0; i < 10050; i++) {
  var h = ((i * 2654435761) >>> 0).toString(16);
  SESSIONS.push({ id: "sess-" + ("00000000" + h).slice(-8) + "-" + i, n: i, created_at: new Date(Date.UTC(2026, 9, 24) - i * 1000).toISOString() });
}
var URLS = [];
function server(method, path) {
  URLS.push(path);
  var m = /[?&]limit=(\d+)&offset=(\d+)/.exec(path), limit = Number(m[1]), offset = Number(m[2]);
  var rows = SESSIONS.slice();
  if (/order_by=created_at:desc/.test(path)) rows.sort(function (a, b) { return a.created_at < b.created_at ? 1 : -1; });
  else rows.sort(function (a, b) { return a.id < b.id ? -1 : 1; });
  return Promise.resolve({ kind: "offset", offset: offset, length: Math.min(limit, rows.length - offset), total: rows.length,
                           items: rows.slice(offset, offset + limit) });
}
window.primerApi.apiFetch = server;
var OUT = null;
function readWith(key) { FETCHERS[key](null).then(function (r) { OUT = r; }); }
"""


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.mark.parametrize("file,mount,key", [
    ("sessions-list.jsx", "MR.mount(SessionsList, {});", "sessions:list"),
    ("toolsets.jsx", "MR.mount(TS_SessionsTab, { id: 'web' });", "toolset-sessions:web"),
])
def test_the_cap_keeps_the_newest_sessions(file, mount, key):
    ctx = mini_react_context(transpile(UI / "components" / file), _PRELUDE)
    try:
        ctx.eval(mount)
        ctx.eval(f"readWith({json.dumps(key)});")
        urls = js(ctx, "URLS")
        assert urls[0] == "/sessions?limit=200&offset=0&order_by=created_at:desc" and len(urls) == 50
        out = js(ctx, "{ n: OUT.items.length, truncated: OUT.truncated, newest: OUT.items[0].n, oldest: OUT.items[OUT.items.length - 1].n }")
        assert out == {"n": 10000, "truncated": True, "newest": 0, "oldest": 9999}, "the 10000 newest, the 50 oldest left out and said"
        assert js(ctx, "PR_pagedNote(OUT, 'sessions')") == "Showing the first 10000 of 10050 sessions."
    finally:
        ctx.close()
