"""Studio revamp: the four-state status language and the attention bar.

ui/studio/STUDIO-WIRING.md §3 (attention bar), §4 (status buckets).

The bucket mapping decides what every rail row, group header and queue count
says, so it is executed for real in MiniRacer rather than substring-matched.
The React surfaces are checked by source + transpile, the ui/ suite convention.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STUDIO = UI / "components" / "studio"
STATUS = STUDIO / "st-status.jsx"
API = STUDIO / "st-api.jsx"
ATTENTION = STUDIO / "st-attention.jsx"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(STATUS.read_text(encoding="utf-8"))
    return ctx


# ---------------------------------------------------------------------------
# §4 - the four-state status language
# ---------------------------------------------------------------------------


def test_status_module_exists_and_exports() -> None:
    assert STATUS.exists()
    src = STATUS.read_text(encoding="utf-8")
    assert "function ST2_bucketOf(" in src
    assert "ST2_BUCKET_ORDER" in src
    assert "window.ST2_bucketOf" in src


def test_bucket_order_is_needs_first() -> None:
    ctx = _ctx()
    assert ctx.eval('ST2_BUCKET_ORDER.join(",")') == "needs,working,broken,done"


def test_parked_asking_a_question_needs_you() -> None:
    ctx = _ctx()
    ctx.eval('var b = ST2_bucketOf({status:"parked", park_reason:"ask_user"}, {});')
    assert ctx.eval("b.bucket") == "needs"
    assert ctx.eval("b.tone") == "--amber"
    assert ctx.eval("b.label") == "needs you"
    assert "question" in ctx.eval("b.detail")


def test_approval_park_names_the_command_when_known() -> None:
    ctx = _ctx()
    ctx.eval('var b = ST2_bucketOf({status:"parked", park_reason:"approval", tool:"shell__run"}, {});')
    assert ctx.eval("b.bucket") == "needs"
    assert "shell__run" in ctx.eval("b.detail")


def test_pending_yield_promotes_a_running_session_to_needs() -> None:
    # A session can be RUNNING and still have an unanswered yield.
    ctx = _ctx()
    ctx.eval('var b = ST2_bucketOf({id:"s1", status:"running"}, {pendingBySession:{s1:true}});')
    assert ctx.eval("b.bucket") == "needs"


def test_pending_yield_promotion_works_on_the_list_endpoint_shape() -> None:
    # GET /workspaces/{wid}/sessions returns SessionInfo, which carries
    # `session_id` and NOT `id`. Resolving only `id` silently never promoted a
    # single row in the rail, because every row comes from that endpoint - and
    # the bug is invisible (a needs-you row just renders as "working").
    ctx = _ctx()
    ctx.eval(
        'var b = ST2_bucketOf({session_id:"s1", status:"running"},'
        " {pendingBySession:{s1:true}});"
    )
    assert ctx.eval("b.bucket") == "needs"


def test_pending_snapshot_for_another_session_does_not_leak() -> None:
    ctx = _ctx()
    ctx.eval(
        'var b = ST2_bucketOf({session_id:"s1", status:"running"},'
        " {pendingBySession:{s2:true}});"
    )
    assert ctx.eval("b.bucket") == "working"


def test_watch_and_sleep_parks_read_as_working() -> None:
    # Technically parked, but nothing waits on a human - so the queue count and
    # the rail's "needs you" group agree.
    ctx = _ctx()
    for reason in ("watch", "watch_files", "sleep"):
        ctx.eval(f'var b = ST2_bucketOf({{status:"parked", park_reason:"{reason}"}}, {{}});')
        assert ctx.eval("b.bucket") == "working", reason
        assert ctx.eval("b.tone") == "--blue", reason


def test_failed_is_broken_and_carries_the_error_code() -> None:
    ctx = _ctx()
    ctx.eval('var b = ST2_bucketOf({status:"failed", ended_detail:"tool_execution_failed", tool:"shell__run"}, {});')
    assert ctx.eval("b.bucket") == "broken"
    assert ctx.eval("b.tone") == "--red"
    assert "tool_execution_failed" in ctx.eval("b.detail")


def test_ended_with_an_error_code_is_broken_not_done() -> None:
    ctx = _ctx()
    ctx.eval('var b = ST2_bucketOf({status:"ended", ended_detail:"routing_failed"}, {});')
    assert ctx.eval("b.bucket") == "broken"


def test_clean_terminal_states_are_done() -> None:
    ctx = _ctx()
    for st in ("completed", "ended", "cancelled"):
        ctx.eval(f'var b = ST2_bucketOf({{status:"{st}"}}, {{}});')
        assert ctx.eval("b.bucket") == "done", st
        assert ctx.eval("b.tone") == "--text-3", st


def test_unmapped_status_is_visible_never_silently_green() -> None:
    ctx = _ctx()
    ctx.eval('var b = ST2_bucketOf({status:"quantum"}, {});')
    assert ctx.eval("b.bucket") == "working"
    assert "quantum" in ctx.eval("b.detail")


def test_grouping_uses_the_fixed_bucket_order() -> None:
    ctx = _ctx()
    ctx.eval(
        """
        var g = ST2_groupSessions([
          {id:"a", status:"running"},
          {id:"b", status:"parked", park_reason:"ask_user"},
          {id:"c", status:"failed"},
          {id:"d", status:"completed"}
        ], {});
        """
    )
    assert ctx.eval('g.needs.map(function(s){return s.id}).join(",")') == "b"
    assert ctx.eval('g.working.map(function(s){return s.id}).join(",")') == "a"
    assert ctx.eval('g.broken.map(function(s){return s.id}).join(",")') == "c"
    assert ctx.eval('g.done.map(function(s){return s.id}).join(",")') == "d"


# ---------------------------------------------------------------------------
# §2.3 - the endpoint surface
# ---------------------------------------------------------------------------


def test_api_names_every_endpoint_this_feature_touches() -> None:
    api = API.read_text(encoding="utf-8")
    assert "/tool_approval/respond" in api
    assert '"approved"' in api and '"rejected"' in api
    assert "/ask_user/respond" in api
    assert "/yields/" in api and "/cancel" in api
    assert "/yields/pending" in api
    assert "/log?limit=" in api
    assert "/node_states" in api


def test_node_states_uses_the_top_level_graph_route() -> None:
    # It lives on compute.py:249 as /graphs/{gid}/runs/{rid}/node_states - NOT
    # a workspace-scoped path. Wiring it under /workspaces would 404.
    api = API.read_text(encoding="utf-8")
    assert '"/graphs/"' in api
    assert "nodeStates: function (graphId, runId" in api


def test_cache_keys_live_in_one_place() -> None:
    # So a mutation's `invalidates` cannot drift from the reader's resource key.
    api = API.read_text(encoding="utf-8")
    assert "keys:" in api
    assert "studio-yields-pending:" in api


def test_no_studio_component_names_a_url_directly() -> None:
    for f in sorted(STUDIO.glob("st-*.jsx")):
        if f.name == "st-api.jsx":
            continue
        body = f.read_text(encoding="utf-8")
        for marker in ('apiFetch("GET"', 'apiFetch("POST"', 'apiFetch("PUT"', 'apiFetch("DELETE"'):
            assert marker not in body, f"{f.name} calls apiFetch directly; route it through ST2_api"


# ---------------------------------------------------------------------------
# §3 - the attention bar
# ---------------------------------------------------------------------------


def test_attention_bar_is_always_mounted_and_never_collapses() -> None:
    # The stated negative ("Nothing needs you") is the feature.
    src = ATTENTION.read_text(encoding="utf-8")
    assert 'data-testid="attention-bar"' in src
    assert 'data-testid="attention-bar-calm"' in src
    assert "Nothing needs you" in src


def test_attention_bar_guards_a_null_tool_call_id() -> None:
    # tool_call_id is nullable; such a park renders but must not POST.
    src = ATTENTION.read_text(encoding="utf-8")
    assert "actionable" in src
    assert "tool_call_id" in src


def test_attention_bar_keeps_the_shipped_testids() -> None:
    # Journeys locate these; they change parent but keep their ids (§13).
    src = ATTENTION.read_text(encoding="utf-8")
    for tid in (
        "action-required", "action-item", "approve", "reject", "respond",
        "cancel-yield", "action-required-count", "attention-count",
        "attention-queue", "attention-queue-item", "unblock-focus",
        "inform-item", "inform-dismiss",
    ):
        assert f'"{tid}"' in src, tid


def test_attention_bar_reuses_the_one_tap_listener() -> None:
    # Never a second EventSource (§0).
    src = ATTENTION.read_text(encoding="utf-8")
    assert "useWorkspaceTapListener" in src
    assert "EventSource" not in src


def _code_only(path: Path) -> str:
    """Source with `//` comment bodies stripped.

    The checks below are about what the code *does*; a comment that explains
    which old pattern was replaced must not trip them.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def test_attention_writes_go_through_use_mutation_with_invalidates() -> None:
    # Replaces the hand-rolled SA_invalidateSessionPending + delayed-refetch pair.
    src = ATTENTION.read_text(encoding="utf-8")
    assert "useMutation" in src
    assert "invalidates" in src
    code = _code_only(ATTENTION)
    assert "setTimeout(refetch" not in code
    assert "SA_invalidateSessionPending" not in code


def test_keyboard_shortcuts_are_scoped_not_global() -> None:
    # j/k/d/c must not eat typing when no overlay is open.
    src = ATTENTION.read_text(encoding="utf-8")
    assert "queueOpen" in src or "focusOpen" in src


def test_studio_mounts_the_bar_behind_the_v2_tweak() -> None:
    studio = (UI / "components" / "studio.jsx").read_text(encoding="utf-8")
    assert "AttentionBar" in studio
    assert "studioV2" in studio
    tweaks = (UI / "foundation" / "tweaks.js").read_text(encoding="utf-8")
    assert "studioV2:" in tweaks


def test_v2_is_strictly_opt_in_while_incomplete() -> None:
    # === true, not !== false: the graph builder shipped inert behind a
    # permissive guard, so this one must refuse to appear until finished.
    studio = (UI / "components" / "studio.jsx").read_text(encoding="utf-8")
    assert "studioV2 === true" in studio


def test_new_files_are_registered_in_index_html_before_studio() -> None:
    lines = (UI / "index.html").read_text(encoding="utf-8").splitlines()
    order = [i for i, ln in enumerate(lines) if 'type="text/babel"' in ln and "src=" in ln]
    def idx(frag: str) -> int:
        for i in order:
            if frag in lines[i]:
                return i
        raise AssertionError(f"{frag} is not registered in index.html")

    assert idx("studio/st-status.jsx") < idx("components/studio.jsx")
    assert idx("studio/st-api.jsx") < idx("components/studio.jsx")
    assert idx("studio/st-attention.jsx") < idx("components/studio.jsx")


def test_every_studio_file_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    for f in sorted(STUDIO.glob("st-*.jsx")):
        rel = f"components/studio/{f.name}"
        code = b._transform(f.read_text(encoding="utf-8"), rel)
        assert code, rel
