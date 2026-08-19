"""The deep-link guard (spec section 6).

URL addressability is a HARD requirement despite the router's death,
because agent open_file, shared links and browser history all depend on
it. So every doc kind and every overlay round-trips, and so do the two
anchor grammars.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-url.js"

# One representative ref per kind, chosen to be hostile: slashes, spaces
# and reserved characters are exactly what a real file path or slug has.
REFS = {
    "session": "sess-4f1a2b3c",
    "file": "src/deep/nested path/api.ts",
    "diff": "9c1f2ab",
    "wiki": "col-1/guides/getting started",
    "trace": "sess-4f1a2b3c:7",
}


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _kinds() -> list[str]:
    return json.loads(_ctx().eval("JSON.stringify(SH_DOC_KINDS)"))


def _overlays() -> list[str]:
    return json.loads(_ctx().eval("JSON.stringify(SH_OVERLAYS)"))


def test_every_doc_kind_has_a_representative_ref() -> None:
    """A kind added without a REFS entry silently skips its round trip."""
    assert set(_kinds()) == set(REFS)


@pytest.mark.parametrize("kind", _kinds())
def test_doc_round_trip(kind: str) -> None:
    ctx = _ctx()
    ref = REFS[kind]
    url = ctx.eval(
        f"SH_buildUrl({{wid: {json.dumps('ws-1')}, "
        f"doc: {{kind: {json.dumps(kind)}, ref: {json.dumps(ref)}}}}})"
    )
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["wid"] == "ws-1"
    assert back["doc"] == {"kind": kind, "ref": ref}


@pytest.mark.parametrize("name", _overlays())
def test_overlay_round_trip(name: str) -> None:
    ctx = _ctx()
    url = ctx.eval(
        f"SH_buildUrl({{wid: {json.dumps('ws-1')}, "
        f"overlay: {{name: {json.dumps(name)}, "
        f"section: {json.dumps('sect')}, id: {json.dumps('id-1')}}}}})"
    )
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["overlay"] == {"name": name, "section": "sect", "id": "id-1"}


@pytest.mark.parametrize(
    "anchor,expected",
    [
        ("turn-42", {"kind": "turn", "turn": 42}),
        ("L10-L30", {"kind": "lines", "from": 10, "to": 30}),
        ("L7", {"kind": "lines", "from": 7, "to": None}),
    ],
)
def test_anchor_round_trip(anchor: str, expected: dict) -> None:
    ctx = _ctx()
    url = ctx.eval(
        'SH_buildUrl({wid: "ws-1", doc: {kind: "session", ref: "s1"}, '
        f"anchor: {json.dumps(anchor)}}})"
    )
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["anchor"] == anchor
    parsed = json.loads(
        ctx.eval(f"JSON.stringify(SH_parseAnchor({json.dumps(anchor)}))")
    )
    assert parsed == expected


def test_a_doc_and_an_overlay_coexist_in_one_url() -> None:
    """Section 8's canonical form carries both at once, which is what a
    pasted link from an overlay-open state looks like."""
    ctx = _ctx()
    url = ctx.eval(
        'SH_buildUrl({wid: "ws-1", doc: {kind: "file", ref: "a/b.ts"}, '
        'overlay: {name: "providers", section: "tts", id: "pv-1"}, '
        'anchor: "L3-L9"})'
    )
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["doc"] == {"kind": "file", "ref": "a/b.ts"}
    assert back["overlay"] == {"name": "providers", "section": "tts", "id": "pv-1"}
    assert back["anchor"] == "L3-L9"
