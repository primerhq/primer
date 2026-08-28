"""One file names URLs, so a route change has one edit site.

Mirrors the one-file-names-the-URLs discipline, and pins
the two endpoints S8 is the FIRST console consumer of: the S1 binding
switch and the S7 turn timeline.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-api.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_in_the_bundle() -> None:
    assert 'src="components/shell/sh-api.jsx"' in (UI / "index.html").read_text(
        encoding="utf-8"
    )
    assert "window.SH_api = SH_api;" in _src()


def test_binding_switch_matches_the_s1_endpoint() -> None:
    """S1 spec section 6: POST /workspaces/{wid}/sessions/{sid}/binding."""
    src = _src()
    assert '"/binding"' in src
    assert re.search(r'switchBinding:\s*function\s*\(wid,\s*sid,\s*binding\)', src)
    assert '"POST"' in src


def test_trace_timeline_matches_the_s7_endpoint() -> None:
    """S7 spec section 5: GET /sessions/{sid}/turns/{turn_no}/timeline."""
    src = _src()
    assert '"/turns/"' in src and '"/timeline"' in src


def test_approval_decisions_use_the_shipped_respond_body() -> None:
    src = _src()
    assert '"/tool_approval/respond"' in src
    assert 'decision: "approved"' in src
    assert 'decision: "rejected"' in src


def _without_comments(body: str) -> str:
    """Strip // line and /* */ block comments.

    The gate is about where URLs are BUILT, and a comment explaining what
    a route used to be is not a second URL site. It tripped on a note
    recording the pre-S8 spelling of a route, which is exactly the kind
    of context worth writing down.
    """
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return re.sub(r"//[^\n]*", "", body)


def test_no_other_shell_file_names_a_v1_url() -> None:
    """A second URL site is how st-api's discipline was worth having."""
    shell = UI / "components" / "shell"
    for path in sorted(shell.glob("*.jsx")):
        if path.name == "sh-api.jsx":
            continue
        body = _without_comments(path.read_text(encoding="utf-8"))
        assert '"/workspaces/' not in body, path.name
        assert '"/sessions/' not in body, path.name


def test_every_read_takes_an_abort_signal() -> None:
    src = _src()
    for name in ("sessions", "session", "filesTree", "fileRead", "commitLog",
                 "commit", "pendingYields", "pendingAttention",
                 "approvalRecords", "timeline"):
        m = re.search(name + r":\s*function\s*\(([^)]*)\)", src)
        assert m, name
        assert m.group(1).strip().endswith("signal"), name


def test_steer_sends_the_field_the_endpoint_declares() -> None:
    """Regression: every send from the composer 422'd.

    SteerBody names the field ``instruction`` because the one endpoint
    covers invoking, steering and resuming a session. The shell sent
    ``content``, which is not a field on that model at all, so sending a
    message -- the single most-used action in the console -- failed on
    every use and said only "Steer failed" in a toast.

    Pinned against the model itself rather than restating the name, so
    the two cannot drift apart again.
    """
    from primer.api.routers.workspaces import SteerBody

    assert "instruction" in SteerBody.model_fields
    assert "content" not in SteerBody.model_fields

    src = _src()
    body = src[src.index("steer: function"):]
    body = body[:body.index("},")]
    assert "instruction: instruction" in body
    assert "content:" not in body


def test_every_shell_request_body_matches_its_endpoint_model() -> None:
    """One sweep, so the next mismatch is caught before it ships.

    Two of these were wrong at once and both broke an everyday action
    silently: steer sent ``content`` where SteerBody declares
    ``instruction``, so every message sent from the composer 422'd, and
    rewind sent ``seq`` where RewindBody declares ``to_seq``. Both showed
    only a generic failure toast.
    """
    from primer.api.routers.tool_approval import ToolApprovalRespondBody
    from primer.api.routers.workspaces import (
        FileWriteBody,
        RewindBody,
        SteerBody,
    )

    src = _src()

    def body_of(fn: str) -> str:
        chunk = src[src.index(f"{fn}: function"):]
        return chunk[:chunk.index("},")]

    cases = [
        ("steer", SteerBody, ["instruction"]),
        ("rewind", RewindBody, ["to_seq"]),
        ("fileWrite", FileWriteBody, ["content", "encoding"]),
        ("approve", ToolApprovalRespondBody, ["tool_call_id", "decision"]),
        ("reject", ToolApprovalRespondBody,
         ["tool_call_id", "decision", "reason"]),
    ]
    for fn, model, sent in cases:
        chunk = body_of(fn)
        for field in sent:
            assert field in model.model_fields, f"{fn} sends {field!r}, " \
                f"which is not a field on {model.__name__}"
            assert f"{field}:" in chunk, f"{fn} no longer sends {field!r}"
