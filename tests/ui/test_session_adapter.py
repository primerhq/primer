"""Session adapter (ui/components/session-adapter.jsx) tests.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_conversation_extracted.py / test_session_live_history.py), plus
one MiniRacer eval of the actual mapping function (mirrors
test_chat_coalesce_forwards_agent_id_and_created_at_from_first_token) so
the load-bearing kind table and divider labels are exercised for real
rather than only substring-matched.

Phase 2 (2026-08-28): the live data hook (SA_useSessionConversation) and
the SA_encodeCursor helper moved to ui/foundation/session-store.js and
ui/foundation/use-workspace-tap.js. The adapter now holds only the pure
record->transcript mapping (SA_toTranscript + the kind tables). The
catch-up / ws-state / transport / controls tests moved to the store/hub
(test_session_store.py); the tests below cover the remaining surface.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
ADAPTER = UI / "components" / "session-adapter.jsx"
INDEX = UI / "index.html"


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_session_adapter_module_exists_and_exports() -> None:
    assert ADAPTER.exists(), "ui/components/session-adapter.jsx is missing"
    src = ADAPTER.read_text(encoding="utf-8")
    assert "function SA_toTranscript(" in src
    assert "window.SA_toTranscript = SA_toTranscript;" in src
    assert "window.SA_KIND_TO_TRANSCRIPT = SA_KIND_TO_TRANSCRIPT;" in src
    assert "window.SA_SKIP_IN_TRANSCRIPT = SA_SKIP_IN_TRANSCRIPT;" in src
    assert "window.SA_diffStatOfResult = SA_diffStatOfResult;" in src
    assert "window.SA_resultCountLabel = SA_resultCountLabel;" in src
    # Phase 2: the data hook + cursor encode moved to the store/hub; the
    # adapter must not re-introduce them.
    assert "SA_useSessionConversation" not in src
    assert "SA_encodeCursor" not in src


def test_kind_mapping_table_matches_the_locked_contract() -> None:
    # Load-bearing mapping (studio-agents-interact Task 11 + Global
    # Constraints' "Transport rules (locked)"): every SessionMessageKind /
    # TapEventClass value must appear with its documented transcript kind.
    src = ADAPTER.read_text(encoding="utf-8")
    expected = {
        "user_input": "user_message",
        "assistant_token": "assistant_message",
        "tool_call": "tool_call",
        "tool_result": "tool_result",
        "graph_transition": "divider",
        "invocation_divider": "divider",
        # Lifecycle rows map to the same-named kinds Message() styles directly
        # (not a collapsed "lifecycle"/"interaction" bucket, which has no
        # Message() branch and renders as a generic agent bubble).
        "yielded": "yielded",
        "resumed": "resumed",
        "done": "done",
        "cancelled": "cancelled",
        "error": "error",
    }
    for kind, transcript_kind in expected.items():
        assert f'{kind}: "{transcript_kind}"' in src, (
            f"SA_KIND_TO_TRANSCRIPT must map {kind!r} -> {transcript_kind!r}"
        )


def test_session_adapter_registered_before_studio_center() -> None:
    order = _order()
    assert "components/session-adapter.jsx" in order
    assert "components/console/nv-session-doc.jsx" in order
    assert order.index("components/session-adapter.jsx") < order.index(
        "components/console/nv-session-doc.jsx"
    )


def test_bundle_transpiles_with_session_adapter() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/session-adapter.jsx === */" in text


def test_sa_to_transcript_maps_records_via_mini_racer() -> None:
    """Runs the real SA_toTranscript/SA_KIND_TO_TRANSCRIPT against a tiny
    sample of SessionMessageRecord-shaped rows, mirroring
    test_chat_coalesce_forwards_agent_id_and_created_at_from_first_token's
    use of py_mini_racer instead of guessing at behavior from a substring
    match.
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "hi"}, created_at: "t1", node_id: null},
          {seq: 2, kind: "graph_transition",
           payload: {node_id: "n1", phase: "enter"}, created_at: "t2", node_id: "n1"},
          {seq: 3, kind: "invocation_divider", payload: {invocation: 3}, created_at: "t3", node_id: null},
          {seq: 4, kind: "done", payload: {}, created_at: "t4", node_id: null},
        ];
        var out = window.SA_toTranscript(records, {id: "s1"});
        """
    )
    assert ctx.eval("out.length") == 4
    assert ctx.eval("out[0].kind") == "user_message"
    # What the row SAYS. This sample has always fed a user_input with
    # payload.text and never checked that it survived the mapping, so the
    # transcript rendered every message row with an empty body: identity
    # chips and nothing beside them, for the operator's own messages and
    # the agent's answers alike.
    assert ctx.eval("out[0].label") == "hi"
    assert ctx.eval("out[1].kind") == "divider"
    assert ctx.eval("out[1].label") == "n1 · enter"
    assert ctx.eval("out[1].nodeId") == "n1"
    assert ctx.eval("out[2].kind") == "divider"
    assert ctx.eval("out[2].label") == "— invocation 3 —"
    # A DONE record maps to Message()'s own "done" kind (muted "· done" row),
    # not a generic "lifecycle" bucket.
    assert ctx.eval("out[3].kind") == "done"


def test_rewind_marker_folds_the_discarded_span_via_mini_racer() -> None:
    """US-008 R3 item 4: a rewind must be VISIBLE (a fold divider, same
    treatment as compaction_marker) and it must hide the span it
    discarded - the /messages read is visible=false by design
    (primer/api/routers/sessions.py), so nothing upstream hides the
    raw rows for us.
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "first"}, created_at: "t1"},
          {seq: 2, kind: "assistant_token", payload: {text: "first answer"}, created_at: "t2"},
          {seq: 3, kind: "user_input", payload: {text: "second"}, created_at: "t3"},
          {seq: 4, kind: "assistant_token", payload: {text: "second answer"}, created_at: "t4"},
          {seq: 5, kind: "rewind_marker", payload: {to_seq: 1, actor: "user"}, created_at: "t5"},
          {seq: 6, kind: "user_input", payload: {text: "third"}, created_at: "t6"},
        ];
        var out = window.SA_toTranscript(records, {id: "s1"});
        """
    )
    # Only the kept turn, the marker's own divider, and the post-rewind
    # turn survive - seq 2/3/4 (the discarded span) do not.
    import json

    seqs = json.loads(ctx.eval(
        "JSON.stringify(out.map(function (r) { return r.seq; }))"
    ))
    assert seqs == [1, 5, 6]
    assert ctx.eval("out[0].label") == "first"
    assert ctx.eval("out[1].kind") == "divider"
    assert "kept up to #1" in ctx.eval("out[1].label")
    assert ctx.eval("out[2].label") == "third"


def test_progressive_rewind_folds_nest_via_mini_racer() -> None:
    """R3 cross-review defect 1 (HIGH): a SECOND rewind must compose with
    the first, not replace it - primer/session/replay.py's visible_records
    docstring: "Rewind, continue, rewind again nests correctly." The prior
    implementation kept only the LAST marker's (to_seq, marker_seq) pair,
    so the FIRST rewind's discarded span (seq 6-10 below) silently
    resurfaced once a second rewind was recorded.

    client_action/llm_call at seq 2-4 are unrelated padding (always
    skipped by SA_SKIP_IN_TRANSCRIPT regardless of any fold) - they make
    the fixture 16 records without adding a THIRD kept span to reason
    about; the two rewinds and their discarded content are the only
    thing under test.
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "first"}, created_at: "t1"},
          {seq: 2, kind: "client_action", payload: {}, created_at: "t2"},
          {seq: 3, kind: "llm_call", payload: {}, created_at: "t3"},
          {seq: 4, kind: "client_action", payload: {}, created_at: "t4"},
          {seq: 5, kind: "assistant_token", payload: {text: "first answer"}, created_at: "t5"},
          {seq: 6, kind: "user_input", payload: {text: "d1"}, created_at: "t6"},
          {seq: 7, kind: "assistant_token", payload: {text: "d2"}, created_at: "t7"},
          {seq: 8, kind: "user_input", payload: {text: "d3"}, created_at: "t8"},
          {seq: 9, kind: "assistant_token", payload: {text: "d4"}, created_at: "t9"},
          {seq: 10, kind: "user_input", payload: {text: "d5"}, created_at: "t10"},
          {seq: 11, kind: "rewind_marker", payload: {to_seq: 5, actor: "user"}, created_at: "t11"},
          {seq: 12, kind: "user_input", payload: {text: "second"}, created_at: "t12"},
          {seq: 13, kind: "assistant_token", payload: {text: "second answer"}, created_at: "t13"},
          {seq: 14, kind: "user_input", payload: {text: "d6"}, created_at: "t14"},
          {seq: 15, kind: "assistant_token", payload: {text: "d7"}, created_at: "t15"},
          {seq: 16, kind: "rewind_marker", payload: {to_seq: 13, actor: "user"}, created_at: "t16"}
        ];
        var out = window.SA_toTranscript(records, {id: "s1"});
        var visible = window.SA_visibleRecords(records);
        """
    )
    import json

    out_seqs = json.loads(ctx.eval(
        "JSON.stringify(out.map(function (r) { return r.seq; }))"
    ))
    assert out_seqs == [1, 5, 11, 12, 13, 16]
    visible_seqs = json.loads(ctx.eval(
        "JSON.stringify(visible.map(function (r) { return r.seq; }))"
    ))
    # SA_visibleRecords keeps rewind_marker itself (unlike the backend,
    # for display) but is otherwise the same walk, so seq 2-4 (real
    # content there, generic-skip is a later, separate stage) survive
    # here even though they don't reach the final transcript.
    assert visible_seqs == [1, 2, 3, 4, 5, 11, 12, 13, 16]


def test_transcript_rows_carry_the_text_they_display() -> None:
    """Every message kind, and the parts shape a realized steer arrives in."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "ask"}, created_at: "t1"},
          {seq: 2, kind: "assistant_token", payload: {text: "answer"}, created_at: "t2"},
          {seq: 3, kind: "user_input",
           payload: {parts: [{type: "text", text: "queued"}]}, created_at: "t3"},
          {seq: 4, kind: "tool_call",
           payload: {name: "workspace__grep", arguments: {}}, created_at: "t4"},
        ];
        var out = window.SA_toTranscript(records, {id: "s1"});
        """
    )
    assert ctx.eval("out[0].label") == "ask"
    assert ctx.eval("out[1].label") == "answer"
    # A steer stored as parts is realized into the same transcript.
    assert ctx.eval("out[2].label") == "queued"
    # A tool call draws its own chip, so it needs no label. MiniRacer
    # hands back JSUndefined rather than None for an absent property.
    assert not ctx.eval("out[3].label")


# ---------------------------------------------------------------------------
# UX reconcile wave 3 (audit A item 6): SA_diffStatOf - the live/done
# "editing {file} +N -N" chip's line-delta data. workspace__edit_file's
# own tool result (primer/workspace/local/tools/edit.py) is a unified
# diff already, via difflib.unified_diff - this counts its content
# lines. Edit-only by construction (it parses diff text, and write never
# produced any); SA_diffStatOfResult below is the wave-5 seam that
# additionally covers write via its own server-computed metadata.
# ---------------------------------------------------------------------------


def _diff_stat_ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(ADAPTER.read_text(encoding="utf-8"))
    return ctx


def test_diff_stat_counts_content_lines_only() -> None:
    ctx = _diff_stat_ctx()
    diff_text = (
        "--- a/src/api.ts\n"
        "+++ b/src/api.ts\n"
        "@@ -4,6 +4,9 @@\n"
        " export const webhookConfig = {\n"
        "+  signingSecret: env(\"STRIPE_WEBHOOK_SECRET\"),\n"
        "+  rotatedAt: \"2026-08-23T09:41:00Z\",\n"
        "-  signingSecret: \"whsec_live_9f31\",\n"
        "   tolerance: 300,\n"
    )
    ctx.eval(f"var out = window.SA_diffStatOf({diff_text!r});")
    out = {
        "additions": ctx.eval("out.additions"),
        "deletions": ctx.eval("out.deletions"),
    }
    assert out == {"additions": 2, "deletions": 1}


def test_diff_stat_ignores_the_filename_header_lines() -> None:
    """"--- a/x" / "+++ b/x" are the diff's own filename pair, not a
    removed/added content line - counting them would over-count by
    exactly one of each on every call."""
    ctx = _diff_stat_ctx()
    diff_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    ctx.eval(f"var out = window.SA_diffStatOf({diff_text!r});")
    assert ctx.eval("out.additions") == 1
    assert ctx.eval("out.deletions") == 1


def test_diff_stat_is_null_for_no_change() -> None:
    ctx = _diff_stat_ctx()
    assert ctx.eval("window.SA_diffStatOf('')") is None
    assert ctx.eval("window.SA_diffStatOf(null)") is None
    assert ctx.eval(
        "window.SA_diffStatOf('--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n unchanged\\n')"
    ) is None


# ---------------------------------------------------------------------------
# UX reconcile wave 5 (audit A item 6, write half): SA_diffStatOfResult -
# the one seam a caller uses for a tool_result row's diff stat regardless
# of which tool produced it (edit's unified-diff text vs write's own
# server-computed metadata, primer/workspace/local/tools/write.py).
# ---------------------------------------------------------------------------


def test_diff_stat_of_result_prefers_write_metadata() -> None:
    ctx = _diff_stat_ctx()
    ctx.eval(
        "var out = window.SA_diffStatOfResult({payload: {"
        "output: 'wrote 9 bytes to a.py',"
        "metadata: {additions: 2, deletions: 1}"
        "}});"
    )
    assert ctx.eval("out.additions") == 2
    assert ctx.eval("out.deletions") == 1


def test_diff_stat_of_result_new_file_metadata_has_zero_deletions() -> None:
    ctx = _diff_stat_ctx()
    ctx.eval(
        "var out = window.SA_diffStatOfResult({payload: {"
        "output: 'wrote 5 bytes to a.py',"
        "metadata: {additions: 3, deletions: 0}"
        "}});"
    )
    assert ctx.eval("out.additions") == 3
    assert ctx.eval("out.deletions") == 0


def test_diff_stat_of_result_falls_back_to_parsing_edit_output() -> None:
    ctx = _diff_stat_ctx()
    ctx.eval(
        "var out = window.SA_diffStatOfResult({payload: {"
        "output: '--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new\\n'"
        "}});"
    )
    assert ctx.eval("out.additions") == 1
    assert ctx.eval("out.deletions") == 1


def test_diff_stat_of_result_is_null_when_neither_source_has_a_diff() -> None:
    ctx = _diff_stat_ctx()
    assert ctx.eval(
        "window.SA_diffStatOfResult({payload: {output: 'src/api.ts:88'}})"
    ) is None
    assert ctx.eval("window.SA_diffStatOfResult({payload: {}})") is None
    assert ctx.eval("window.SA_diffStatOfResult(null)") is None


# ---------------------------------------------------------------------------
# UX reconcile wave 5 (audit A item 4): SA_resultCountLabel - the
# "searched N files" chip label for a grep tool_result row, read from
# its own exact match_count/file_count/truncated metadata
# (primer/workspace/local/tools/grep.py) rather than a client-side parse
# of the (possibly head_limit-capped) output text.
# ---------------------------------------------------------------------------


def test_result_count_label_singular_file() -> None:
    ctx = _diff_stat_ctx()
    ctx.eval(
        "var out = window.SA_resultCountLabel({payload: {"
        "metadata: {match_count: 2, file_count: 1, truncated: false}"
        "}});"
    )
    assert ctx.eval("out") == "searched 1 file"


def test_result_count_label_plural_files() -> None:
    ctx = _diff_stat_ctx()
    ctx.eval(
        "var out = window.SA_resultCountLabel({payload: {"
        "metadata: {match_count: 7, file_count: 4, truncated: false}"
        "}});"
    )
    assert ctx.eval("out") == "searched 4 files"


def test_result_count_label_honest_about_truncation() -> None:
    """A capped output list must never present a partial count as if it
    were exact - the "250+" marker, not "250" or "4"."""
    ctx = _diff_stat_ctx()
    ctx.eval(
        "var out = window.SA_resultCountLabel({payload: {"
        "metadata: {match_count: 312, file_count: 250, truncated: true}"
        "}});"
    )
    assert ctx.eval("out") == "searched 250+ files"


def test_result_count_label_is_null_when_metadata_absent() -> None:
    """A mid-flight tool_result (executor hasn't attached metadata yet)
    or a pre-wave-5 persisted record - the caller keeps its existing
    input-arg chip form in both cases."""
    ctx = _diff_stat_ctx()
    assert ctx.eval(
        "window.SA_resultCountLabel({payload: {output: 'src/api.ts:88'}})"
    ) is None
    assert ctx.eval("window.SA_resultCountLabel({payload: {}})") is None
    assert ctx.eval("window.SA_resultCountLabel(null)") is None


def test_result_count_label_is_null_for_non_grep_metadata() -> None:
    """write's {additions, deletions} metadata has no file_count - must
    not be mistaken for a grep row."""
    ctx = _diff_stat_ctx()
    assert ctx.eval(
        "window.SA_resultCountLabel({payload: {"
        "metadata: {additions: 2, deletions: 1}"
        "}})"
    ) is None
