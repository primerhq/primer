"""The session doc is where five section-8 rules land at once.

The composer NEVER locks during a run (Enter queues a steer, rendered as
a chip at its insertion point); the status string comes from the same
function the rail and the tab use, so all three altitudes agree; scroll
anchoring is the shared decision function, never a forced scroll; turns
render two-phase with nested subagents and plain-language chips; and the
binding chip posts the S1 endpoint rather than inventing one.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-session-doc.jsx"
API = UI / "components" / "shell" / "sh-api.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_in_the_bundle() -> None:
    assert 'src="components/shell/sh-session-doc.jsx"' in (
        UI / "index.html"
    ).read_text(encoding="utf-8")
    assert "window.SH_SessionDoc = SH_SessionDoc;" in _src()


def test_the_composer_never_locks_during_a_run() -> None:
    """Prohibited: a composer that goes read-only while the agent works."""
    src = _src()
    assert "disabled={false}" in src, (
        "the composer's disabled prop must be a literal false, so no later "
        "edit can quietly bind it to the running flag"
    )
    assert not re.search(r"disabled=\{[^}]*running", src)


def test_the_composer_itself_is_the_reused_one() -> None:
    """Pinned decision 16: Composer is reused verbatim."""
    assert "window.Composer" in _src()


def test_enter_queues_a_steer_and_the_queue_is_rendered() -> None:
    src = _src()
    assert "SH_api.steer" in src
    assert 'data-testid={"shell-queued-steer:"' in src
    assert "pending_messages" in src


def test_a_queued_chip_renders_parts_not_a_content_field() -> None:
    """PendingSessionMessage carries `parts`; there is no `content`."""
    src = _src()
    assert "row.parts" in src or "(row.parts || [])" in src
    assert ".content" not in src


def test_a_queued_chip_can_be_dismissed() -> None:
    """Shipped dispatch realizes a queued row only on the clean-completion
    exit, so a chip left by a failed or cancelled turn never resolves."""
    src = _src()
    assert 'data-testid={"shell-queued-steer-dismiss:"' in src
    assert "dismissQueuedSteer" in src


def test_the_detail_row_is_flat_and_the_agent_comes_off_the_binding() -> None:
    """GET /sessions/{sid} is response_model=WorkspaceSession."""
    src = _src()
    assert "detail.data.session" not in src, (
        "there is no session sub-object on the response"
    )
    assert "session.agent_id" not in src, (
        "WorkspaceSession has no top-level agent_id; it is binding.agent_id"
    )
    assert "binding.agent_id" in src


def test_the_status_string_comes_from_the_shared_function() -> None:
    src = _src()
    assert "SH_statusLine" in src and "SH_statusFromTap" in src
    assert 'data-testid="shell-composer-status"' in src


def test_the_status_is_mounted_before_the_first_token() -> None:
    """Prohibited: a silent pre-first-token gap or a bare spinner."""
    src = _src()
    assert "optimisticStart" in src, (
        "send must mount a status locally rather than waiting for the "
        "first tap frame to arrive"
    )


def test_scroll_anchoring_uses_the_shared_decision_not_a_local_rule() -> None:
    src = _src()
    assert "SH_scrollDecision" in src
    assert 'data-testid="shell-jump-latest"' in src
    assert "scrollIntoView" not in src, (
        "force-follow scroll is an explicit antipattern; the decision "
        "function owns whether the viewport moves"
    )


def test_the_binding_chip_posts_the_s1_endpoint() -> None:
    src = _src()
    assert 'data-testid="shell-binding-chip"' in src
    assert "SH_api.switchBinding" in src


def test_rewind_and_compact_reached_the_api_module() -> None:
    """Spec section 4 lists rewind/compact among the S1 contracts."""
    api = API.read_text(encoding="utf-8")
    # The third parameter's NAME is incidental; what matters is that the
    # call exists and takes a sequence. Its body is pinned against
    # RewindBody in test_shell_api, which is where the field name that
    # actually goes over the wire belongs.
    assert re.search(r"rewind:\s*function\s*\(wid,\s*sid,\s*\w+\)", api)
    assert re.search(r"compact:\s*function\s*\(wid,\s*sid\)", api)
    assert '"/rewind"' in api and '"/compact"' in api


def test_session_verbs_declare_a_pointer_surface() -> None:
    src = _src()
    for verb in ("session.rewind", "session.compact", "session.switchBinding",
                 "session.jumpLatest", "session.steer"):
        assert verb in src, verb
    for block in re.findall(r"surfaces:\s*\[([^\]]*)\]", src):
        assert block.strip() != '"palette"', block


def test_the_slash_affordance_reuses_the_palette_rows() -> None:
    """One registry, two entry points."""
    src = _src()
    assert "SH_PaletteRows" in src
    assert 'charAt(0) === "/"' in src


def test_turn_rendering_is_two_phase_with_nesting_and_chips() -> None:
    src = _src()
    assert "SH_collapseTurns" in src
    assert "SH_nestSubagentRows" in src
    assert "SH_toolChipLabel" in src
    assert 'data-testid={"shell-turn:"' in src


def test_identity_chips_are_stable_and_non_human() -> None:
    """Prohibited: human-passing agent identities."""
    src = _src()
    assert "SH_IdentityChip" in src
    assert "SH_GLYPHS" in src, "the glyph set must be a literal, not a name"
    assert "on behalf of" in src


def test_raw_tool_arguments_never_render_inline() -> None:
    src = _src()
    assert "JSON.stringify(row.payload" not in src
    assert ".arguments" not in src, (
        "raw args belong to the Trace tab; the chip speaks plain language"
    )


def test_the_session_poll_follows_the_session_it_is_watching() -> None:
    """A flat cadence was wrong in both directions.

    A CREATED session is about to change and the operator is watching for
    exactly that transition, so five seconds is a visible lag. An ENDED
    session will never change again, and polling it forever costs a
    request every five seconds per open tab for news that cannot come.
    """
    src = _src()
    block = src[src.index("var terminalRef = React.useRef(false);"):]
    block = block[:block.index("var history")]
    assert "pollMs: terminalRef.current ? 0 : 2000" in block, (
        "poll while the session is live, and stop once it is over"
    )
    assert "SH_sessionIsOver(detail.data)" in block, (
        "and decide that from the row the poll just returned"
    )


def test_the_transcript_refreshes() -> None:
    """Regression: the session transcript was fetched once and never again.

    It was declared with pollMs: 0 and nothing called refetch on it, so
    sending a message and watching the answer arrive -- the whole loop
    this document exists for -- only worked if you reloaded the page.
    """
    src = _src()
    block = src[src.index('SH_api.keys.session(sid) + ":messages"'):]
    block = block[:block.index("var gates")]
    assert "pollMs: terminalRef.current ? 0 : 2000" in block, (
        "poll the transcript while the session is live, stop when it is over"
    )
    assert "history.refetch();" in src, (
        "and refresh it on send, so the operator sees their own message "
        "without waiting out a poll interval"
    )
