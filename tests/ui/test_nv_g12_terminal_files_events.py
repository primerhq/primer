"""Red probes for the G12 terminal/files/events console batch (G12-02/03/04/06).

The audit (/var/tmp/revamp/audit/G12.md) found four console gaps:

* G12-02 the terminal's workspace-gone close code (4404, the server's own
  workspace_not_found) is bucketed into the TRANSIENT "Connection lost.
  Usually transient." + Retry state, whose button can never win;
* G12-03 a FAILED files-tree fetch renders as the EMPTY workspace
  ("no files yet." + a New-file write into a workspace whose state failed to
  read), with no error copy and no retry;
* G12-04 the diff tab renders "Empty commit." while the commit fetch is
  still loading (and again when it fails);
* G12-06 the workspace Events panel names a session on every row but offers
  no deep link, is desktop-only, and swallows a failed history backfill
  silently behind "Connected. Waiting for events...".

The auditor's probes in /var/tmp/revamp/audit/G12/ (02/03/04/06) pin the
exact red contracts these mirror: MiniRacer over the COMMITTED expressions
with SCALAR returns, plus static pins on the committed source.
"""

from __future__ import annotations

import json
import re

import pytest


def _paths() -> dict:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return {
        "terminal": (root / "ui/components/console/nv-terminal.jsx").read_text(encoding="utf-8"),
        "sidebar": (root / "ui/components/console/nv-files-sidebar.jsx").read_text(encoding="utf-8"),
        "filedocs": (root / "ui/components/console/nv-file-docs.jsx").read_text(encoding="utf-8"),
        "tap": (root / "ui/components/workspace-tap.jsx").read_text(encoding="utf-8"),
        "workspaces": (root / "ui/components/workspaces.jsx").read_text(encoding="utf-8"),
    }


OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while OPEN_CONTEXTS:
        OPEN_CONTEXTS.pop().close()


def _mr():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    OPEN_CONTEXTS.append(ctx)
    return ctx


# ---- G12-02: the terminal's workspace-gone close is permanent ------------------


def _onclose_calls(code: int) -> list:
    """Eval the committed onclose handler with the server's real codes
    (terminal.py: 4401 auth, 4403 role, 4404 workspace_not_found, 1011
    internal), the auditor's probe contract, plus the new permanent state."""
    src = _paths()["terminal"]
    m = re.search(r"sock\.onclose = function \(ev\) \{.*?\n    \};", src, re.S)
    assert m, "onclose handler not found"
    handler = m.group(0)
    ctx = _mr()
    ctx.eval(
        "var calls = [];"
        "var setDenied = function (v) { calls.push(['denied', v]); };"
        "var setSignedOut = function (v) { calls.push(['signedOut', v]); };"
        "var setConnLost = function (v) { calls.push(['connLost', v]); };"
        "var setWorkspaceGone = function (v) { calls.push(['workspaceGone', v]); };"
        "var cancelled = false; var gotExit = false;"
    )
    ctx.eval("var sock_onclose = " + handler.replace("sock.onclose = ", "", 1))
    ctx.eval("sock_onclose({ code: %d });" % code)
    return json.loads(ctx.eval("JSON.stringify(calls)"))


def test_terminal_onclose_keeps_the_existing_codes_and_4404_lands_permanent() -> None:
    assert _onclose_calls(4403) == [["denied", True]]
    assert _onclose_calls(4401) == [["signedOut", True]]
    # A gone workspace is permanent: 4404 must not land in the transient
    # conn-lost bucket, whose Retry re-closes with 4404 and can never win.
    assert _onclose_calls(4404) == [["workspaceGone", True]]
    # A bare blip / internal-error close still reads as transient:
    assert _onclose_calls(1011) == [["connLost", True]]


def test_the_workspace_gone_state_renders_the_gone_view_without_retry() -> None:
    src = _paths()["terminal"]
    assert "workspaceGone ? (" in src, "no render branch for the permanent state"
    start = src.rindex("workspaceGone ? (")
    branch = src[start : src.index(") :", start)]
    assert "NV_WorkspaceGone" in branch, "the branch should reuse the surface's gone view"
    assert "nv-terminal-retry" not in branch, "a permanent state carries no Retry"
    assert "setWorkspaceGone(false)" in src, "the state must clear when the effect re-runs"


# ---- G12-03: a failed tree fetch is not an empty workspace ---------------------


def _sidebar_body() -> str:
    src = _paths()["sidebar"]
    start = src.index("function NV_FilesSidebar(")
    nxt = src.find("\nwindow.", start)
    return src[start : nxt if nxt != -1 else len(src)]


def test_the_files_sidebar_error_branch_is_selected_on_a_failed_fetch() -> None:
    body = _sidebar_body()
    cm = re.search(r"\((tree\.error && !tree\.data)\)", body)
    assert cm, "the committed component has no tree.error branch"
    cond = cm.group(1)
    for shape, expected in (
        ("{ data: null, error: { status: 500, title: 'boom' } }", True),
        ("{ data: { items: [] }, error: null }", False),
    ):
        ctx = _mr()
        ctx.eval("var tree = %s;" % shape)
        out = json.loads(ctx.eval("JSON.stringify(%s)" % cond))
        assert out is expected, f"the error branch mis-selects under {shape}"


def test_the_files_error_branch_says_why_and_offers_a_retry() -> None:
    src = _paths()["sidebar"]
    assert 'data-testid="nv-files-tree-error"' in src, "no error branch in the committed component"
    region = src[src.index('data-testid="nv-files-tree-error"') - 300 : src.index('data-testid="nv-files-tree-error"') + 500]
    assert "refetch" in region, "the error branch must offer the refetch"
    assert 'data-testid="nv-files-tree-error-retry"' in region


# ---- G12-04: "Empty commit." only for a loaded zero-file commit -----------------


def _diffdoc_body() -> str:
    src = _paths()["filedocs"]
    start = src.index("function NV_DiffDoc(")
    nxt = src.find("\nfunction ", start)
    return src[start : nxt if nxt != -1 else len(src)]


def _empty_commit_condition() -> str:
    """The committed condition of the "Empty commit." branch (the line that
    opens it), as it reads today."""
    body = _diffdoc_body()
    lines = body.splitlines()
    idx = [k for k, l in enumerate(lines) if "Empty commit." in l][0]
    cm = re.match(r"^\s*\{((?:commit\.data != null && )?!files\.length && [^{]*?)\? \($", lines[idx - 1])
    assert cm, f"the Empty-commit branch condition not found: {lines[idx - 1]!r}"
    return cm.group(1)


def test_the_empty_commit_branch_only_fires_on_loaded_data() -> None:
    cond = _empty_commit_condition()
    for name, shape, expected in (
        ("loading", "null", False),
        ("error", "null", False),
        ("empty_commit", "{}", True),
        ("files_hidden", '{"files_hidden": true}', False),
        ("real_files", '{"files": [{"path": "a"}]}', False),
    ):
        ctx = _mr()
        ctx.eval("var commit = { data: %s };" % shape)
        ctx.eval("var files = (commit.data && commit.data.files) || [];")
        out = json.loads(ctx.eval("JSON.stringify(%s)" % cond))
        assert out is expected, f"{name}: the Empty-commit branch reads {out}"


def test_the_diff_doc_names_a_failed_or_loading_commit_read() -> None:
    body = _diffdoc_body()
    assert "commit.error" in body, "the committed doc has no commit.error branch"
    assert "Loading commit" in body, "the committed doc has no loading line"


# ---- G12-06: the events panel links sessions, reaches phones, says its failures -


def test_the_tap_event_row_deep_links_the_session() -> None:
    src = _paths()["tap"]
    row = re.search(r'data-testid="tap-event-row".*?</div>', src, re.S).group(0)
    assert "/sessions/" in row, "the row's session cell must deep-link like every sibling surface"
    assert "navigate" in row


def test_the_mobile_workspace_tabs_include_events() -> None:
    src = _paths()["workspaces"]
    mobile = re.search(r"const mobileTabs = \[(.*?)\];", src, re.S).group(1)
    ids = re.findall(r'\{ id: "([a-z]+)"', mobile)
    assert "events" in ids, ids
    desktop = re.search(r"const tabs = \[(.*?)\];", src, re.S).group(1)
    assert '{ id: "events"' in desktop, "desktop Events tab drifted away"


def test_the_tap_history_backfill_failure_is_said() -> None:
    src = _paths()["tap"]
    m = re.search(r"\)\.catch\(function \((\w*)\) \{(.*?)\}\);", src, re.S)
    assert m, "the backfill catch not found"
    assert m.group(1), "the catch ignores the error it caught"
    assert "best-effort" not in m.group(2), "the silent swallow stands"
    assert "setHistoryError" in m.group(2), "the catch records no failure state"
    assert "Could not load event history" in src, "no visible failure copy"
    assert 'data-testid="tap-history-retry"' in src, "no retry affordance"
