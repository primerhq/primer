"""The session row's launch-time base prompt is written only where it is pinned (Wiki First W4).

``base_prompt`` is one passive field: the create writes it, the three
assembly sites read it (agent, graph, manual compaction), and nothing else
touches it. There is no update or patch session route, so the immutability
is by construction - this pin is the guard against the construction being
broken: any module in ``primer/`` that names the field must be on the
pinned list, and a listed module that stops naming it fails (the field
moved somewhere the pin does not name). It is the field-level sibling of
``tests/session/test_session_writer_set.py`` (which pins the whole-document
writers of the row, of which this field adds none).
"""

from __future__ import annotations

from tests._support.session_writer_scan import default_root

# Every module in primer/ that may name ``base_prompt``: the model (the
# field), the create path (REST body, the shared factory, the MCP tool arg)
# and the three assembly sites (agent, graph, manual compaction). A new
# reader or writer of the field names a module that fails this pin.
PINNED_FILES = frozenset({
    "model/workspace_session.py",
    "api/routers/sessions.py",
    "workspace/session_factory.py",
    "toolset/workspaces.py",
    "agent/workspace_executor.py",
    "graph/workspace_executor.py",
    "worker/executor_builders.py",
    "api/routers/workspaces.py",
})


def test_only_the_pinned_modules_name_the_base_prompt() -> None:
    root = default_root()
    found = {
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if "base_prompt" in path.read_text(encoding="utf-8")
    }

    assert found == PINNED_FILES, (
        "the session row's base prompt is written only where it is pinned. "
        f"named by code: {sorted(found)}; pinned: {sorted(PINNED_FILES)}"
    )
