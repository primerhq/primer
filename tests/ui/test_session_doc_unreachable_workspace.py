"""A workspace that cannot be read is not an empty conversation (architecture review A-08, 2026-10-08).

The messages read answers a typed 503 (``/errors/workspace-unreachable``) when the workspace's runtime does not answer, where it used
to answer an empty list. The session document says so, in words, with a way to retry, instead of drawing an empty conversation.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _problem():
    from py_mini_racer import MiniRacer

    start = DOC.index("function NV_historyProblem")
    end = DOC.index("\n}\n", start) + len("\n}\n")
    ctx = MiniRacer()
    ctx.eval(DOC[start:end])
    return lambda error: json.loads(ctx.eval("JSON.stringify(NV_historyProblem(" + json.dumps(error) + "))"))


def test_an_unreachable_workspace_is_said_so_and_other_failures_keep_their_old_handling() -> None:
    problem = _problem()
    got = problem({"status": 503, "type": "/errors/workspace-unreachable", "title": "Workspace Unreachable", "detail": "x"})
    assert got["kind"] == "unreachable"
    assert "unreachable" in got["text"].lower() and "not lost" in got["text"].lower()
    assert "workspace" in got["next"].lower()
    assert problem(None) is None
    assert problem({"status": 500, "type": "/errors/internal"}) is None, "only this typed error gets the banner"
    assert problem({"status": 503, "type": "/errors/service-unavailable"}) is None


def test_the_session_document_shows_the_problem_banner_with_a_retry() -> None:
    assert "NV_historyProblem(history.error)" in DOC
    at = DOC.index('data-testid="nv-history-problem"')
    banner = DOC[at - 300:at + 900]
    assert 'role="alert"' in banner
    assert "history.refetch()" in banner
    assert at < DOC.index('className="nv-transcript-split"'), "the banner sits above the transcript, not inside it"
