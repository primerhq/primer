"""Review 2026-11-23 finding 5.1: the local workspace ``write`` tool must not
create files under ``.state/.git/``.

A local workspace's git repository lives under ``.state/.git``, and the
per-turn commit (``primer/workspace/local/state.py`` ``_run_git_once``) runs
``git`` with no curated environment and no ``core.hooksPath`` override -- so a
hook an agent wrote with the ``write`` tool (no reserved-tree check) is
executed by primer's own git with the API process's full environment (Postgres
DSN, ``PRIMER_SECRET_*``, provider keys).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.workspace  # noqa: F401  (ToolCallContext model_rebuild)
from primer.workspace.local.tools import Write, WriteArgs

_CTX = SimpleNamespace(
    session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None)
)


@pytest.mark.asyncio
async def test_write_refuses_a_git_hook_inside_state_dot_git(tmp_path: Path) -> None:
    """Writing ``.state/.git/hooks/post-commit`` must be refused (an error
    result or an exception), and the hook file must not exist."""
    root = tmp_path / "ws"
    (root / ".state" / ".git").mkdir(parents=True)
    hook = root / ".state" / ".git" / "hooks" / "post-commit"

    raised: Exception | None = None
    result = None
    try:
        result = await Write(root).execute(
            WriteArgs(
                path=".state/.git/hooks/post-commit",
                content="#!/bin/sh\nenv > leak.txt\n",
                mode="0755",
            ),
            _CTX,
        )
    except Exception as exc:  # a refusal may be a tool error or a raised exception
        raised = exc

    assert raised is not None or not hook.exists(), (
        "the write tool created a git hook under .state/.git (result: "
        f"{getattr(result, 'output', None)!r}); primer's per-turn commit "
        "executes that hook with the API process's full environment"
    )
