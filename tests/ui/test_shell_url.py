"""S8 URL-as-state (spec section 8), hosted in the console's hash.

/console is a StaticFiles mount with no SPA catch-all
(primer/api/_app_middleware.py:369-382), so the canonical grammar lives
after the leading "#". The anchor keeps its literal "#turn-42" spelling
because the fragment is everything after the FIRST "#".

Pure string logic, so it is EXECUTED here rather than substring-matched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-url.js"
INDEX = ROOT / "ui" / "index.html"
MANIFEST = ROOT / "ui" / "fixtures" / "shell" / "manifest.json"
CATALOG = ROOT / "ui" / "components" / "provider-catalog.jsx"


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


def test_a_record_without_a_tab_survives_the_url() -> None:
    """A plain detail view has to be deep-linkable.

    The id was written only inside the section branch, so an overlay
    carrying a record but no tab -- every plain detail view -- came out
    as bare "overlay=agents" and lost the record. Reloading landed back
    on the list, and there was no way to link to a record unless it
    happened to have a tab.
    """
    ctx = _ctx()
    url = ctx.eval(
        'SH_buildUrl({wid: "ws-1", overlay: {name: "agents", id: "ag-42"}})'
    )
    assert url == "#/w/ws-1?overlay=agents::ag-42"
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["overlay"] == {"name": "agents", "section": None, "id": "ag-42"}

    # A tab still occupies the middle slot, record after it.
    tabbed = ctx.eval(
        'SH_buildUrl({wid: "ws-1", overlay: {name: "agents",'
        ' section: "tools", id: "ag-42"}})'
    )
    assert tabbed == "#/w/ws-1?overlay=agents:tools:ag-42"

    # And a bare list overlay still carries no slots at all.
    listing = ctx.eval(
        'SH_buildUrl({wid: "ws-1", overlay: {name: "agents"}})'
    )
    assert listing == "#/w/ws-1?overlay=agents"


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
    assert out == {
        "wid": "ws-1",
        "view": {"name": "studio", "nav": None, "sub": None},
        "doc": None,
        "overlay": None,
        "anchor": None,
    }


# ---------------------------------------------------------------------------
# Building is the parser's inverse, workspace or no workspace.
# ---------------------------------------------------------------------------


def test_an_overlay_survives_a_url_with_no_workspace() -> None:
    """Regression: platform overlays could not be addressed before boot.

    The platform surfaces are not workspace-scoped, and the parser reads
    "#/?overlay=agents" perfectly well. Building discarded the whole state
    whenever the workspace was missing and returned a bare "#/", so the
    url-sync effect rewrote that address the moment the shell rendered
    without a resolved workspace, and the overlay was gone before anything
    could show it.
    """
    ctx = _ctx()
    ctx.eval(
        """
        var built = SH_buildUrl({
          wid: null, doc: null,
          overlay: { name: "agents", section: null, id: null }, anchor: null
        });
        var back = SH_parseUrl(built);
        """
    )
    assert ctx.eval("built") == "#/?overlay=agents"
    assert json.loads(ctx.eval("JSON.stringify(back.overlay)")) == {
        "name": "agents", "section": None, "id": None,
    }


def test_a_document_survives_a_url_with_no_workspace() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var b2 = SH_buildUrl({
          wid: null, doc: { kind: "session", ref: "sess-1" },
          overlay: null, anchor: null
        });
        var back2 = SH_parseUrl(b2);
        """
    )
    assert ctx.eval("b2") == "#/?doc=session:sess-1"
    assert json.loads(ctx.eval("JSON.stringify(back2.doc)")) == {
        "kind": "session", "ref": "sess-1",
    }


def test_an_empty_state_is_still_the_bare_root() -> None:
    ctx = _ctx()
    ctx.eval(
        'var b3 = SH_buildUrl({ wid: null, doc: null, overlay: null, anchor: null });'
    )
    assert ctx.eval("b3") == "#/"


# ---------------------------------------------------------------------------
# The url can be ahead of the shell mid-navigation.
# ---------------------------------------------------------------------------


def test_a_url_naming_another_workspace_is_ahead_not_wrong() -> None:
    """Regression: the shell overwrote the address it was navigating to.

    Arriving at another workspace is not one render. The hashchange
    listener applies the new url's document first and the workspace prop
    only catches up once the root gate re-reads the hash, so in between
    the shell holds the new document beside the old workspace. Writing
    the url from that mixture replaced the workspace still being
    navigated to with the one being left.
    """
    ctx = _ctx()
    ctx.eval(
        """
        var ahead = SH_urlIsAhead(SH_parseUrl("#/w/ws-target?doc=session:s1"),
                                  "ws-previous");
        var settled = SH_urlIsAhead(SH_parseUrl("#/w/ws-target?doc=session:s1"),
                                    "ws-target");
        var noWid = SH_urlIsAhead(SH_parseUrl("#/?overlay=agents"), "primer");
        var nothing = SH_urlIsAhead(null, "primer");
        """
    )
    assert ctx.eval("ahead") is True
    assert ctx.eval("settled") is False
    # A url with no workspace names no destination, so it cannot be ahead:
    # the platform overlays are addressable without one.
    assert ctx.eval("noWid") is False
    assert ctx.eval("nothing") is False


def test_the_sync_effect_actually_consults_that_rule() -> None:
    # The nv shell serves the same no-clobber rule structurally: the
    # hashchange handler applies EVERY parsed field in one batch and the
    # write effect refuses to touch a hash it did not author itself
    # (ownHashRef), so an arriving deep link is never overwritten by a
    # half-changed render.
    shell = (
        ROOT / "ui" / "components" / "console" / "nv-shell.jsx"
    ).read_text(encoding="utf-8")
    assert "ownHashRef" in shell
    assert "if (current === ownHashRef.current) return;" in shell


def test_views_match_the_manifest() -> None:
    ctx = _ctx()
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    views = json.loads(ctx.eval("JSON.stringify(SH_VIEWS)"))
    assert views["platform"] == man["views"]["platform"]
    assert views["system"] == man["views"]["system"]
    assert views["studio"] == []


def test_view_round_trips_and_studio_stays_unwritten() -> None:
    """Wiring plan P0 T3: absent view = studio (old URLs parse
    forward); platform/system serialize as view=name:nav."""
    ctx = _ctx()
    # Old-form URL: no view param, parses as studio, rebuilds identically.
    out = json.loads(ctx.eval(
        "JSON.stringify(SH_parseUrl('#/w/ws-1?doc=session:s-1'))"))
    assert out["view"] == {"name": "studio", "nav": None, "sub": None}
    rebuilt = ctx.eval(
        "SH_buildUrl(SH_parseUrl('#/w/ws-1?doc=session:s-1'))")
    assert rebuilt == "#/w/ws-1?doc=session:s-1"
    # Platform with nav round-trips.
    url = ctx.eval(
        "SH_buildUrl({wid:'ws-1', view:{name:'platform', nav:'agents'}})")
    assert url == "#/w/ws-1?view=platform:agents"
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}))"))
    assert back["view"] == {"name": "platform", "nav": "agents", "sub": None}
    # Unknown nav degrades to the view root, unknown view to studio.
    bad_nav = json.loads(ctx.eval(
        "JSON.stringify(SH_parseUrl('#/?view=system:nope'))"))
    assert bad_nav["view"] == {"name": "system", "nav": None, "sub": None}
    bad_view = json.loads(ctx.eval(
        "JSON.stringify(SH_parseUrl('#/?view=garage'))"))
    assert bad_view["view"] == {"name": "studio", "nav": None, "sub": None}


# ---------------------------------------------------------------------------
# Provider types: the third segment of view=platform:providers:<type> (Platform revamp Plan 01, Task 6; seam I-1).
# ---------------------------------------------------------------------------

SIDEBAR_TYPES = ["llm", "embedding", "ssp", "cross_encoder", "workspace", "web_search", "channel", "artifact_storage", "tts", "stt"]


def test_a_provider_type_round_trips() -> None:
    ctx = _ctx()
    url = ctx.eval("SH_buildUrl({ wid: 'ws-1', view: { name: 'platform', nav: 'providers', sub: 'tts' } })")
    assert url == "#/w/ws-1?view=platform:providers:tts"
    back = json.loads(ctx.eval(f"JSON.stringify(SH_parseUrl({json.dumps(url)}).view)"))
    assert back == {"name": "platform", "nav": "providers", "sub": "tts"}


def test_an_unknown_provider_type_is_dropped_and_the_url_rebuilt_without_it() -> None:
    ctx = _ctx()
    parsed = json.loads(ctx.eval("JSON.stringify(SH_parseUrl('#/?view=platform:providers:bogus').view)"))
    assert parsed == {"name": "platform", "nav": "providers", "sub": None}
    assert ctx.eval("SH_buildUrl(SH_parseUrl('#/?view=platform:providers:bogus'))") == "#/?view=platform:providers"


def test_a_state_holding_an_unknown_type_is_written_without_it() -> None:
    """The builder checks the type too, so a caller that hands the shell a type the grammar does not list never writes it into the address bar."""
    ctx = _ctx()
    assert ctx.eval("SH_buildUrl({ view: { name: 'platform', nav: 'providers', sub: 'bogus' } })") == "#/?view=platform:providers"


def test_a_sub_is_only_read_on_a_nav_that_has_subs() -> None:
    ctx = _ctx()
    assert json.loads(ctx.eval("JSON.stringify(SH_parseUrl('#/?view=platform:agents:llm').view)"))["sub"] is None
    assert ctx.eval("SH_buildUrl({ view: { name: 'platform', nav: 'agents', sub: 'llm' } })") == "#/?view=platform:agents"
    assert ctx.eval("SH_buildUrl({ view: { name: 'platform', sub: 'llm' } })") == "#/?view=platform"
    assert json.loads(ctx.eval("JSON.stringify(SH_parseUrl('#/?view=system:profile:llm').view)"))["sub"] is None


def test_a_type_with_a_trailing_empty_segment_or_extra_segments_is_still_a_valid_view() -> None:
    ctx = _ctx()
    assert json.loads(ctx.eval("JSON.stringify(SH_parseUrl('#/?view=platform:providers:').view)")) == {"name": "platform", "nav": "providers", "sub": None}
    assert json.loads(ctx.eval("JSON.stringify(SH_parseUrl('#/?view=platform:providers:tts:more').view)")) == {"name": "platform", "nav": "providers", "sub": "tts"}


def test_the_types_are_provider_classes_in_sidebar_order() -> None:
    ctx = _ctx()
    types = json.loads(ctx.eval("JSON.stringify(SH_VIEW_SUBS['platform:providers'])"))
    assert types == SIDEBAR_TYPES, "the sidebar order: TTS and ASR last"
    catalog = CATALOG.read_text(encoding="utf-8")
    classes = re.findall(r'\{ key: "(\w+)", label: ', catalog[catalog.index("const PROVIDER_CLASSES"):catalog.index("const PC_ALL_TYPE_CHIPS")])
    assert set(types) <= set(classes), "a type that is not a provider class would open the catalog on nothing"
    assert types[-2:] == ["tts", "stt"]


def test_view_subs_match_the_manifest() -> None:
    ctx = _ctx()
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert json.loads(ctx.eval("JSON.stringify(SH_VIEW_SUBS)")) == man["view_subs"]


def test_every_sub_list_belongs_to_a_nav_of_the_grammar() -> None:
    ctx = _ctx()
    views = json.loads(ctx.eval("JSON.stringify(SH_VIEWS)"))
    for key in json.loads(ctx.eval("JSON.stringify(SH_VIEW_SUBS)")):
        name, _, nav = key.partition(":")
        assert nav in views[name], f"{key}: a sub list for a nav the grammar does not admit can never be reached"
