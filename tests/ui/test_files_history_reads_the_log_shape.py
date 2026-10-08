"""Files > History reads the commit log the way the API serves it (console review 2026-10-08, C-026).

``GET /v1/workspaces/{wid}/log`` answers ``{"commits": [...]}`` (tests/api/test_workspaces.py pins the key), but both Files
sidebars read ``commits.data.items``, so History said "No turn commits yet." for every workspace and the commit-diff tab was
unreachable from it. The fixture-less source test that passed ("commitLog" in the sidebar) could not see it; these feed the real
response shape through the real api module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
API = ROOT / "ui" / "components" / "shell" / "sh-api.jsx"
FILES_SIDEBAR = (CONSOLE / "nv-files-sidebar.jsx").read_text(encoding="utf-8")
MOBILE_SHELL = (CONSOLE / "nv-mobile-shell.jsx").read_text(encoding="utf-8")

# What the log route serves for one workspace with two turn commits (CommitInfo.model_dump, with_files=1).
REAL_LOG_BODY = {
    "commits": [
        {"sha": "a" * 40, "subject": "turn[2] write notes.md", "committed_at": "2026-10-08T10:00:00Z", "workspace_id": "primer",
         "session_id": "sess-1", "agent_id": "operator", "op": "write", "tool": "files__write", "call_id": "c2",
         "files": [{"path": "notes.md", "additions": 3, "deletions": 0, "binary": False}]},
        {"sha": "b" * 40, "subject": "turn[1] init", "committed_at": "2026-10-08T09:59:00Z", "workspace_id": "primer",
         "session_id": None, "agent_id": None, "op": None, "tool": None, "call_id": None, "files": None},
    ]
}


@pytest.fixture(scope="module")
def api():
    """One V8 isolate for the module, closed on teardown (tests/ui leaks undisposed isolates otherwise)."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = { apiFetch: function () {} };")
    ctx.eval(API.read_text(encoding="utf-8"))
    yield ctx
    ctx.close()


def _rows(ctx, body) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify(window.SH_api.commitRows(" + json.dumps(body) + "))"))


def test_the_rows_of_the_real_log_response_are_its_commits(api) -> None:
    rows = _rows(api, REAL_LOG_BODY)
    assert [r["subject"] for r in rows] == ["turn[2] write notes.md", "turn[1] init"]
    assert rows[0]["sha"] == "a" * 40 and rows[0]["session_id"] == "sess-1" and rows[0]["op"] == "write"


def test_an_items_shaped_body_is_not_a_commit_log(api) -> None:
    """The list envelope other routes use is NOT the log's: a fixture shaped like it is what hid this bug."""
    assert _rows(api, {"items": REAL_LOG_BODY["commits"]}) == []


def test_a_missing_or_empty_log_has_no_rows(api) -> None:
    for body in (None, {}, {"commits": []}, {"commits": None}):
        assert _rows(api, body) == [], body


def test_both_sidebars_take_their_rows_from_the_log_helper() -> None:
    for name, src in (("nv-files-sidebar", FILES_SIDEBAR), ("nv-mobile-shell", MOBILE_SHELL)):
        assert "SH_api.commitRows(commits.data)" in src, name
        assert not re.search(r"commits\.data\s*&&\s*commits\.data\.items", src), f"{name} still reads the wrong key"


def test_the_mobile_files_tab_without_a_workspace_resolves_an_empty_log_in_the_real_shape() -> None:
    assert "Promise.resolve({ commits: [] })" in MOBILE_SHELL
    assert "SH_api.commitLog(wid, 50, signal) : Promise.resolve({ items: [] })" not in MOBILE_SHELL
