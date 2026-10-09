"""A commit trailer's value cannot add a trailer line (security ticket 01a11fbc-ffea).

The state repo's trailers are written as ``f"{key}: {value}"`` lines of the commit message, and the history parser reads them back by line. A value
holding CR or LF (the graph's ``X-Primer-Graph-Ended-Detail`` comes from a failure code) injected lines of its own: ``X-Primer-Graph: forged`` became
a trailer of the commit. ``commit_arbitrary`` of both backends now writes each trailer on one line (CR and LF in a key or a value become a space).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from primer.workspace.local.state import LocalStateRepo
from primer.workspace.sandbox.state import _build_arbitrary_message

FORGED = "rate_limited\nX-Primer-Graph: forged\r\nX-Primer-Workspace: other"


def test_the_sandbox_message_builder_writes_each_trailer_on_one_line():
    message = _build_arbitrary_message(subject="s", workspace_id="ws", trailers={"X-Primer-Graph-Ended-Detail": FORGED, "X-Primer-Graph-Status": "failed"})

    lines = message.split("\n")
    assert not any(line.startswith("X-Primer-Graph:") for line in lines), lines
    assert sum(line.startswith("X-Primer-Workspace:") for line in lines) == 1, lines
    assert "X-Primer-Graph-Status: failed" in lines
    assert any(line.startswith("X-Primer-Graph-Ended-Detail: rate_limited") for line in lines)


def test_a_trailer_without_a_newline_is_written_as_it_was():
    message = _build_arbitrary_message(subject="s", workspace_id="ws", trailers={"X-Primer-Graph-Status": "ended"})

    assert message.split("\n") == ["s", "", "X-Primer-Workspace: ws", "X-Primer-Graph-Status: ended"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")
@pytest.mark.asyncio
async def test_a_local_commit_cannot_gain_a_trailer_from_a_trailer_value(tmp_path: Path):
    repo = LocalStateRepo(tmp_path / ".state", workspace_id="ws-test")
    await repo.initialize()

    await repo.commit_arbitrary(
        summary="graph g: state", files={"graphs/g/state.json": "{}"},
        trailers={"X-Primer-Graph-Ended-Detail": FORGED, "X-Primer-Graph-Status": "failed"},
    )

    message = subprocess.check_output(["git", "-C", str(repo.path), "log", "-1", "--format=%B"], text=True)
    lines = message.splitlines()
    assert not any(line.startswith("X-Primer-Graph:") for line in lines), lines
    assert [line for line in lines if line.startswith("X-Primer-Workspace:")] == ["X-Primer-Workspace: ws-test"], lines
    assert "X-Primer-Graph-Status: failed" in lines


def test_a_session_commit_message_cannot_gain_a_trailer_from_a_provider_tool_call_id():
    """``X-Primer-Call`` carries the provider's raw tool_call_id; the session-scoped message builder writes it the same way."""
    from primer.workspace.state_helpers import build_message

    message = build_message(
        subject="s", workspace_id="ws", session_id="sess-1", agent_id="ag", op="tool", tool="fs__read",
        call_id="call_0\nX-Primer-Session: forged X-Primer-Agent: forged",
    )

    lines = message.splitlines()
    assert [line for line in lines if line.startswith("X-Primer-Session:")] == ["X-Primer-Session: sess-1"], lines
    assert [line for line in lines if line.startswith("X-Primer-Agent:")] == ["X-Primer-Agent: ag"], lines
    assert any(line.startswith("X-Primer-Call: call_0") for line in lines)
