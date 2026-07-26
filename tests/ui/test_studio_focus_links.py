"""Studio revamp: ?focus=<tool_call_id> deep links (WIRING §9, §2.3).

The feature is a URL you paste into Slack, which means the link outlives the
yield it points at. Almost every interesting case is therefore the STALE one, so
that is what these tests concentrate on: a shared link whose request was already
answered has to land somewhere useful and say so, never on an empty panel and
never on the wrong item.

The parse/serialise core is pure string work precisely so it can run here for
real. URL / URLSearchParams are Web APIs, not ECMAScript - code built on them
evaluates to nothing in V8, so a query-string edge case written against them
would look tested while asserting on a swallowed exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
ATT = UI / "components" / "studio" / "st-attention.jsx"


def _code_only(src: str) -> str:
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def _ctx():
    """MiniRacer with the pure focus helpers loaded - no window, no Web APIs."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    src = ATT.read_text(encoding="utf-8")
    start = src.index("function ST2_splitHash(hash)")
    end = src.index("function ST2_focusFromUrl()")
    ctx.eval(src[start:end])
    start2 = src.index("function ST2_resolveFocusTarget(")
    end2 = src.index("\n}\n", start2) + 3
    ctx.eval(src[start2:end2])
    return ctx


def test_v8_really_lacks_the_web_apis_this_avoids() -> None:
    # Guards the reason the helpers are pure: if a future runtime gains these,
    # the constraint is gone, but until then anything built on them is untested.
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    assert ctx.eval("typeof URL") == "undefined"
    assert ctx.eval("typeof URLSearchParams") == "undefined"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_focus_is_read_from_the_hash_query() -> None:
    # The Studio is a hash router, so ?focus= lives after the '#'.
    ctx = _ctx()
    assert ctx.eval('ST2_focusFromHash("#/workspaces/ws-1?focus=tc-42")') == "tc-42"


def test_no_focus_param_reads_as_null() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_focusFromHash("#/workspaces/ws-1?open=session:s1")') is None


def test_a_hash_with_no_query_reads_as_null() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_focusFromHash("#/workspaces/ws-1")') is None


def test_an_empty_hash_reads_as_null() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_focusFromHash("")') is None
    assert ctx.eval("ST2_focusFromHash(null)") is None


def test_focus_coexists_with_the_pane_params() -> None:
    ctx = _ctx()
    h = "#/workspaces/ws-1?open=session:s1&aside=file:a.py&focus=tc-9"
    assert ctx.eval(f'ST2_focusFromHash("{h}")') == "tc-9"


def test_focus_is_found_regardless_of_position() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_focusFromHash("#/w?focus=tc-1&open=session:s")') == "tc-1"
    assert ctx.eval('ST2_focusFromHash("#/w?open=session:s&focus=tc-1")') == "tc-1"


def test_an_empty_focus_value_reads_as_null() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_focusFromHash("#/w?focus=")') is None


# ---------------------------------------------------------------------------
# Serialising
# ---------------------------------------------------------------------------


def test_setting_focus_adds_the_param() -> None:
    ctx = _ctx()
    out = ctx.eval('ST2_hashWithFocus("#/workspaces/ws-1", "tc-7")')
    assert out == "#/workspaces/ws-1?focus=tc-7"


def test_clearing_focus_removes_the_param_and_the_lone_question_mark() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_hashWithFocus("#/workspaces/ws-1?focus=tc-7", null)') == "#/workspaces/ws-1"


def test_setting_focus_preserves_the_open_tab_param() -> None:
    # Losing ?open= here would close the reader's tab as a side effect.
    ctx = _ctx()
    out = ctx.eval('ST2_hashWithFocus("#/workspaces/ws-1?open=session:s1", "tc-3")')
    assert "focus=tc-3" in out
    assert "open=session" in out
    assert ctx.eval(f'ST2_focusFromHash("{out}")') == "tc-3"


def test_clearing_focus_preserves_the_route_and_other_params() -> None:
    ctx = _ctx()
    out = ctx.eval('ST2_hashWithFocus("#/workspaces/ws-1?open=session:s1&focus=tc-3", null)')
    assert out.startswith("#/workspaces/ws-1?")
    assert "open=session" in out
    assert "focus" not in out


def test_resetting_focus_replaces_rather_than_duplicates() -> None:
    ctx = _ctx()
    out = ctx.eval('ST2_hashWithFocus("#/w?focus=old", "new")')
    assert out.count("focus=") == 1
    assert ctx.eval(f'ST2_focusFromHash("{out}")') == "new"


def test_a_round_trip_preserves_every_param() -> None:
    ctx = _ctx()
    ctx.eval("""
        var h = "#/workspaces/ws-1?open=session:s1&aside=file:a.py";
        var withFocus = ST2_hashWithFocus(h, "tc-1");
        var back = ST2_hashWithFocus(withFocus, null);
        var openStill = ST2_focusFromHash(withFocus);
    """)
    assert ctx.eval("openStill") == "tc-1"
    # Values are re-encoded, so compare by parsing rather than by string.
    back = ctx.eval("back")
    assert "open=session%3As1" in back
    assert "aside=file%3Aa.py" in back
    assert "focus" not in back


def test_a_tool_call_id_needing_escaping_survives_the_round_trip() -> None:
    ctx = _ctx()
    ctx.eval("""
        var out = ST2_hashWithFocus("#/w", "tc/1 2&3");
        var got = ST2_focusFromHash(out);
    """)
    assert ctx.eval("got") == "tc/1 2&3"


# ---------------------------------------------------------------------------
# Resolution - the stale case is the common one
# ---------------------------------------------------------------------------


def test_a_live_id_opens_that_item() -> None:
    ctx = _ctx()
    ctx.eval(
        'var r = ST2_resolveFocusTarget("tc-2",'
        ' [{tool_call_id: "tc-1"}, {tool_call_id: "tc-2"}], false);'
    )
    assert ctx.eval("r.kind") == "focus"
    assert ctx.eval("r.item.tool_call_id") == "tc-2"


def test_an_already_answered_id_is_stale_not_a_silent_empty_panel() -> None:
    ctx = _ctx()
    ctx.eval('var r = ST2_resolveFocusTarget("tc-gone", [{tool_call_id: "tc-1"}], false);')
    assert ctx.eval("r.kind") == "stale"


def test_an_unknown_id_never_falls_through_to_a_different_item() -> None:
    # Opening the wrong request and letting someone approve it would be the
    # worst possible outcome here.
    ctx = _ctx()
    ctx.eval('var r = ST2_resolveFocusTarget("nonsense", [{tool_call_id: "tc-1"}], false);')
    assert ctx.eval("r.kind") == "stale"
    assert ctx.eval("!!r.item") is False


def test_nothing_is_decided_while_the_snapshot_is_still_loading() -> None:
    # Deciding "stale" against an empty in-flight list would flash the resolved
    # note on every cold load of a perfectly good link.
    ctx = _ctx()
    ctx.eval('var r = ST2_resolveFocusTarget("tc-1", [], true);')
    assert ctx.eval("r.kind") == "wait"


def test_an_empty_queue_after_loading_is_stale() -> None:
    ctx = _ctx()
    ctx.eval('var r = ST2_resolveFocusTarget("tc-1", [], false);')
    assert ctx.eval("r.kind") == "stale"


def test_no_focus_param_is_a_distinct_outcome_from_stale() -> None:
    ctx = _ctx()
    ctx.eval("var r = ST2_resolveFocusTarget(null, [{tool_call_id: 'tc-1'}], false);")
    assert ctx.eval("r.kind") == "none"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_deep_link_is_applied_once_per_url_value() -> None:
    # Focusing writes the URL and the URL is read back as a deep link, so
    # without a guard the two effects feed each other.
    src = _code_only(ATT.read_text(encoding="utf-8"))
    assert "appliedFocusRef" in src
    assert "appliedFocusRef.current === tcid" in src


def test_the_resolver_waits_for_the_snapshot_before_deciding() -> None:
    src = _code_only(ATT.read_text(encoding="utf-8"))
    assert 'target.kind === "wait"' in src
    assert "att.loading" in src


def test_a_stale_link_opens_the_queue_and_clears_the_param() -> None:
    src = _code_only(ATT.read_text(encoding="utf-8"))
    assert "queue: true, focus: null, staleFocus: tcid" in src
    # Leaving the dead id in the address bar would re-trigger on reload.
    assert "ST2_syncFocusUrl(null)" in src


def test_the_resolved_note_is_rendered_in_the_queue() -> None:
    src = ATT.read_text(encoding="utf-8")
    assert '"focus-resolved-note"' in src
    assert "already handled" in src
    # Both queue call sites (calm bar and loud bar) must pass it.
    assert src.count("staleFocus={ui.staleFocus}") == 2


def test_closing_the_queue_clears_the_stale_note() -> None:
    src = ATT.read_text(encoding="utf-8")
    assert src.count("focus: null, staleFocus: null") >= 2


def test_the_url_helpers_keep_the_web_api_at_the_boundary() -> None:
    # Only these two touch history/clipboard; the logic they call is pure.
    src = _code_only(ATT.read_text(encoding="utf-8"))
    assert "ST2_hashWithFocus(window.location.hash, tcid)" in src
    assert "window.history.replaceState" in src
    assert "URLSearchParams" not in src
    assert "new URL(" not in src


def test_copy_link_does_not_navigate() -> None:
    # Building the link must not move the reader off the panel they are
    # answering, so ST2_focusLinkFor never touches history.
    src = ATT.read_text(encoding="utf-8")
    start = src.index("function ST2_focusLinkFor(")
    body = src[start:src.index("\n}\n", start)]
    assert "replaceState" not in body
    assert "ST2_hashWithFocus" in body


def test_copy_link_reports_a_failed_copy_instead_of_claiming_success() -> None:
    # navigator.clipboard is unavailable outside a secure context; a silent
    # no-op there would have the user paste stale clipboard content.
    src = ATT.read_text(encoding="utf-8")
    assert '"focus-copy-link"' in src
    assert "navigator.clipboard" in src
    assert 'title: "Could not copy"' in src
    assert "detail: link" in src


def test_focus_helpers_are_exported() -> None:
    src = ATT.read_text(encoding="utf-8")
    for fn in ("ST2_focusFromUrl", "ST2_syncFocusUrl", "ST2_resolveFocusTarget",
               "ST2_focusLinkFor", "ST2_focusFromHash", "ST2_hashWithFocus"):
        assert f"window.{fn} = {fn};" in src, fn


def test_attention_module_still_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    assert b._transform(ATT.read_text(encoding="utf-8"), "components/studio/st-attention.jsx")
