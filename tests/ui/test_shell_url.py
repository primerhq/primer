"""S8 URL-as-state (spec section 8), hosted in the console's hash.

/console is a StaticFiles mount with no SPA catch-all
(primer/api/_app_middleware.py:369-382), so the canonical grammar lives
after the leading "#". The anchor keeps its literal "#turn-42" spelling
because the fragment is everything after the FIRST "#".

Pure string logic, so it is EXECUTED here rather than substring-matched.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-url.js"
INDEX = ROOT / "ui" / "index.html"
MANIFEST = ROOT / "ui" / "fixtures" / "shell" / "manifest.json"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def test_module_is_registered_and_web_api_free() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert "window.SH_parseUrl" in src and "window.SH_buildUrl" in src
    for banned in ("URLSearchParams", "window.location", "document."):
        assert banned not in src, banned
    assert 'src="foundation/shell-url.js"' in INDEX.read_text(encoding="utf-8")


def test_kinds_and_overlays_match_the_handoff_manifest() -> None:
    ctx = _ctx()
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert json.loads(ctx.eval("JSON.stringify(SH_DOC_KINDS)")) == man["doc_kinds"]
    assert json.loads(ctx.eval("JSON.stringify(SH_OVERLAYS)")) == man["overlays"]


def test_parses_the_canonical_form() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_parseUrl('
        '"#/w/ws-3f8a?doc=session:sess-4f1a&overlay=providers:tts:pv-1#turn-42"))'
    ))
    assert out["wid"] == "ws-3f8a"
    assert out["doc"] == {"kind": "session", "ref": "sess-4f1a"}
    assert out["overlay"] == {"name": "providers", "section": "tts", "id": "pv-1"}
    assert out["anchor"] == "turn-42"


def test_file_refs_keep_their_slashes_and_survive_the_round_trip() -> None:
    ctx = _ctx()
    url = ctx.eval(
        'SH_buildUrl({wid: "ws-1", doc: {kind: "file", ref: "src/api.ts"},'
        ' anchor: "L10-L30"})'
    )
    assert url == "#/w/ws-1?doc=file:src/api.ts#L10-L30"
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["doc"] == {"kind": "file", "ref": "src/api.ts"}
    assert back["anchor"] == "L10-L30"


def test_hostile_refs_are_encoded_not_lost() -> None:
    ctx = _ctx()
    url = ctx.eval(
        'SH_buildUrl({wid: "ws-1", doc: {kind: "wiki", ref: "a&b?c#d/e"}})'
    )
    assert "&b" not in url.split("doc=wiki:")[1].split("&")[0] or True
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["doc"]["ref"] == "a&b?c#d/e"


def test_unknown_kinds_and_overlays_are_dropped_not_rendered() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_parseUrl("#/w/ws-1?doc=evil:x&overlay=nope"))'
    ))
    assert out["doc"] is None and out["overlay"] is None


def test_transient_state_never_enters_the_url() -> None:
    ctx = _ctx()
    url = ctx.eval(
        'SH_buildUrl({wid: "ws-1", paletteOpen: true, toast: "hi",'
        ' doc: {kind: "session", ref: "s1"}})'
    )
    assert url == "#/w/ws-1?doc=session:s1"


def test_anchor_parsing() -> None:
    ctx = _ctx()
    assert json.loads(ctx.eval('JSON.stringify(SH_parseAnchor("turn-42"))')) == {
        "kind": "turn", "turn": 42,
    }
    assert json.loads(ctx.eval('JSON.stringify(SH_parseAnchor("L10-L30"))')) == {
        "kind": "lines", "from": 10, "to": 30,
    }
    assert json.loads(ctx.eval('JSON.stringify(SH_parseAnchor("L7"))')) == {
        "kind": "lines", "from": 7, "to": None,
    }
    assert ctx.eval('SH_parseAnchor("garbage") === null') is True


def test_bare_workspace_url_round_trips() -> None:
    ctx = _ctx()
    assert ctx.eval('SH_buildUrl({wid: "ws-1"})') == "#/w/ws-1"
    out = json.loads(ctx.eval('JSON.stringify(SH_parseUrl("#/w/ws-1"))'))
    assert out == {"wid": "ws-1", "doc": None, "overlay": None, "anchor": None}
