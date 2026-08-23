"""S8 P1: the designer's input package is a manifest plus one fixture per surface.

The manifest is the contract between this plan and the designer: a fixture
that is not listed is invisible to the handoff, and a listed file that does
not exist is a broken handoff. Both directions are asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "ui" / "fixtures" / "shell"
MANIFEST = FIXTURES / "manifest.json"

DOC_KINDS = ["session", "file", "diff", "wiki", "trace", "inbox"]
OVERLAYS = [
    "providers", "collections", "agents", "graphs", "triggers",
    "toolsets", "tools", "workers", "approvals", "admin",
    "harnesses", "services", "channels", "workspaces", "new-session",
    "internal-collections",
    # The Activity events console (2026-08-23 revamp).
    "activity",
]


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_exists_and_parses() -> None:
    assert MANIFEST.is_file(), "the designer package needs a manifest"
    man = _manifest()
    assert set(man) == {"surfaces", "doc_kinds", "overlays"}


def test_manifest_pins_the_doc_kinds_and_overlay_names() -> None:
    man = _manifest()
    assert man["doc_kinds"] == DOC_KINDS
    assert man["overlays"] == OVERLAYS


def test_every_listed_surface_file_exists() -> None:
    man = _manifest()
    for entry in man["surfaces"]:
        assert set(entry) == {"id", "file", "describes"}
        assert (FIXTURES / entry["file"]).is_file(), entry["file"]
        assert entry["describes"].strip(), entry["id"]


def test_no_orphan_fixture_files() -> None:
    listed = {e["file"] for e in _manifest()["surfaces"]}
    on_disk = {p.name for p in FIXTURES.glob("*.json")} - {"manifest.json"}
    assert on_disk == listed, f"unlisted: {on_disk - listed}; missing: {listed - on_disk}"


def test_every_fixture_is_valid_json() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))


def test_session_detail_validates_against_the_flat_WorkspaceSession() -> None:
    """A fixture that drifts from the model teaches the designer a lie.

    GET /v1/sessions/{sid} is response_model=WorkspaceSession and returns
    the row FLAT; pending_messages is the one M14 sibling on top of it.
    """
    from primer.model.workspace_session import WorkspaceSession

    detail = json.loads((FIXTURES / "session-detail.json").read_text(encoding="utf-8"))
    assert "session" not in detail, (
        "there is no session sub-object; the row IS the envelope"
    )
    WorkspaceSession.model_validate(
        {k: v for k, v in detail.items() if k != "pending_messages"}
    )


def test_session_list_rows_validate_against_SessionInfo() -> None:
    from primer.model.workspace_session import SessionInfo, SessionStatus

    for row in json.loads(
        (FIXTURES / "session-list.json").read_text(encoding="utf-8")
    )["items"]:
        SessionInfo.model_validate(row)
        # "parked" is not a lifecycle state; it is the orthogonal
        # parked_status on WorkspaceSession, which SessionInfo lacks.
        assert row["status"] in {s.value for s in SessionStatus}


def test_tap_fixture_rows_validate_against_TapEvent() -> None:
    from primer.tap.event import TapEvent

    rows = json.loads((FIXTURES / "tap-frames.json").read_text(encoding="utf-8"))
    assert rows, "the tap fixture must carry frames"
    for row in rows:
        TapEvent.model_validate(row)


def test_session_detail_carries_the_queued_steer_read_surface() -> None:
    """Amendment M14: the queued-steer chips consume this read surface."""
    from primer.model.workspace_session import PendingSessionMessage

    detail = json.loads((FIXTURES / "session-detail.json").read_text(encoding="utf-8"))
    assert isinstance(detail["pending_messages"], list)
    assert detail["pending_messages"], "at least one queued steer to design against"
    for row in detail["pending_messages"]:
        # parts, never content: store_pending_steer writes
        # parts=[{"type": "text", "text": text}].
        assert set(row) >= {"id", "session_id", "parts", "enqueued_at", "created_at"}
        assert "content" not in row
        PendingSessionMessage.model_validate(row)


def test_the_parked_signal_rides_parked_status_not_the_lifecycle() -> None:
    detail = json.loads((FIXTURES / "session-detail.json").read_text(encoding="utf-8"))
    assert "parked_status" in detail


def test_every_doc_kind_has_at_least_one_fixture() -> None:
    """One doc kind with no stub data is one doc kind the designer guesses at."""
    man = _manifest()
    by_kind = {
        "session": "session-detail",
        "file": "file-read",
        "diff": "commit-diff",
        "wiki": "wiki-document",
        # The trace doc renders S7's timeline, whose fixture Task 4 already
        # shipped for the attention surfaces.
        "trace": "turn-timeline",
        # The Inbox renders the pending feed; its stub is the same
        # pending-yields fixture the attention engine consumes.
        "inbox": "pending-yields",
    }
    ids = {e["id"] for e in man["surfaces"]}
    for kind in man["doc_kinds"]:
        assert by_kind[kind] in ids, f"doc kind {kind} has no fixture"


def test_diff_fixture_carries_per_file_patches() -> None:
    """st-api.jsx:58-65: the commit read is the only source of patch text."""
    commit = json.loads((FIXTURES / "commit-diff.json").read_text(encoding="utf-8"))
    assert commit["sha"]
    assert commit["files"], "a diff doc with no files teaches nothing"
    for f in commit["files"]:
        assert set(f) >= {"path", "patch", "additions", "deletions"}


def test_attention_and_boot_fixtures_carry_the_gating_facts() -> None:
    """Voice gating, role gating and the setup gate are all designable."""
    caps = json.loads((FIXTURES / "capabilities.json").read_text(encoding="utf-8"))
    assert set(caps["speech"]) == {"stt_configured", "tts_configured"}

    auth = json.loads((FIXTURES / "auth-status.json").read_text(encoding="utf-8"))
    assert set(auth) >= {
        "has_user", "authenticated", "username", "role",
        "setup_complete", "setup_missing",
    }

    yields = json.loads((FIXTURES / "pending-yields.json").read_text(encoding="utf-8"))
    kinds = {row["tool_name"] for row in yields["items"]}
    assert "ask_approval" in kinds and "ask_user" in kinds, (
        "attention needs both a decision card and a question to design against"
    )
