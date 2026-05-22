"""Unit tests for the ``watch_files`` yielding tool.

Covers the tool surface only — the LocalWorkspaceWatcher backend has
its own integration tests in ``tests/bus/test_watcher.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from matrix.model.yield_ import (
    ToolContext,
    YieldCancelled,
    YieldTimeout,
    YieldToWorker,
    Yielded,
)
from matrix.toolset.workspaces import build_workspaces_toolset


class _NoopWorkspaceRegistry:
    """The tool only needs the workspace_id from ctx; registry is unused."""

    async def aclose(self):
        return


class _NoopStorageProvider:
    def get_storage(self, _cls):  # pragma: no cover — never called
        raise NotImplementedError


@pytest.fixture
def workspaces_toolset():
    return build_workspaces_toolset(
        storage_provider=_NoopStorageProvider(),
        workspace_registry=_NoopWorkspaceRegistry(),
    )


@pytest.mark.asyncio
class TestWatchFilesHandler:
    async def test_returns_yielded_with_watch_event_key(
        self, workspaces_toolset,
    ):
        ctx = ToolContext(
            tool_call_id="tc-w",
            session_id="sess-w",
            workspace_id="ws-w",
        )
        with pytest.raises(YieldToWorker) as exc_info:
            await workspaces_toolset.call(
                tool_name="watch_files",
                arguments={"paths": ["src/main.py", "docs/README.md"]},
                ctx=ctx,
            )
        y = exc_info.value.yielded
        assert isinstance(y, Yielded)
        assert y.tool_name == "watch_files"
        assert y.event_key == "watch:sess-w:tc-w"
        assert y.resume_metadata["paths"] == ["src/main.py", "docs/README.md"]
        assert y.resume_metadata["batch_window_ms"] == 250
        assert "registered_at_iso" in y.resume_metadata

    async def test_handler_honours_explicit_timeout(self, workspaces_toolset):
        ctx = ToolContext(
            tool_call_id="tc-t",
            session_id="sess-t",
            workspace_id="ws-t",
        )
        with pytest.raises(YieldToWorker) as exc_info:
            await workspaces_toolset.call(
                tool_name="watch_files",
                arguments={
                    "paths": ["a.txt"],
                    "timeout_seconds": 120.0,
                },
                ctx=ctx,
            )
        assert exc_info.value.yielded.timeout == 120.0

    async def test_handler_honours_custom_batch_window(
        self, workspaces_toolset,
    ):
        ctx = ToolContext(
            tool_call_id="tc-b",
            session_id="sess-b",
            workspace_id="ws-b",
        )
        with pytest.raises(YieldToWorker) as exc_info:
            await workspaces_toolset.call(
                tool_name="watch_files",
                arguments={"paths": ["a"], "batch_window_ms": 1000},
                ctx=ctx,
            )
        assert exc_info.value.yielded.resume_metadata["batch_window_ms"] == 1000

    async def test_handler_requires_session_id(self, workspaces_toolset):
        ctx = ToolContext(
            tool_call_id="tc-x",
            session_id=None,
            workspace_id="ws-x",
        )
        result = await workspaces_toolset.call(
            tool_name="watch_files",
            arguments={"paths": ["a"]},
            ctx=ctx,
        )
        assert result.is_error is True
        body = json.loads(result.output)
        assert "session_id" in body["message"]

    async def test_handler_requires_workspace_id(self, workspaces_toolset):
        # Need workspace_id so the watcher knows which workspace's
        # filesystem root to resolve.
        ctx = ToolContext(
            tool_call_id="tc-x",
            session_id="sess-x",
            workspace_id=None,
        )
        result = await workspaces_toolset.call(
            tool_name="watch_files",
            arguments={"paths": ["a"]},
            ctx=ctx,
        )
        assert result.is_error is True
        body = json.loads(result.output)
        assert "workspace_id" in body["message"]

    async def test_handler_rejects_empty_paths_list(self, workspaces_toolset):
        ctx = ToolContext(
            tool_call_id="tc-e",
            session_id="sess-e",
            workspace_id="ws-e",
        )
        result = await workspaces_toolset.call(
            tool_name="watch_files",
            arguments={"paths": []},
            ctx=ctx,
        )
        assert result.is_error is True

    async def test_handler_rejects_absolute_paths(self, workspaces_toolset):
        # Workspace-relative only — absolute paths could escape the
        # sandbox.
        ctx = ToolContext(
            tool_call_id="tc-abs",
            session_id="sess-abs",
            workspace_id="ws-abs",
        )
        result = await workspaces_toolset.call(
            tool_name="watch_files",
            arguments={"paths": ["/etc/passwd"]},
            ctx=ctx,
        )
        assert result.is_error is True
        body = json.loads(result.output)
        assert "absolute" in body["message"].lower()

    async def test_handler_rejects_path_traversal(self, workspaces_toolset):
        ctx = ToolContext(
            tool_call_id="tc-tr",
            session_id="sess-tr",
            workspace_id="ws-tr",
        )
        result = await workspaces_toolset.call(
            tool_name="watch_files",
            arguments={"paths": ["../../../etc/passwd"]},
            ctx=ctx,
        )
        assert result.is_error is True
        body = json.loads(result.output)
        assert "traversal" in body["message"].lower() or ".." in body["message"]


@pytest.mark.asyncio
class TestWatchFilesResumeHook:
    async def test_resume_with_real_changes(self):
        from matrix.toolset.workspaces import watch_files_resume

        meta = {
            "paths": ["a.txt"],
            "batch_window_ms": 250,
            "registered_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        payload = {
            "changes": [
                {
                    "path": "a.txt",
                    "event_type": "modified",
                    "mtime_after": "2026-05-22T10:00:00+00:00",
                },
            ],
        }
        result = watch_files_resume(meta, payload)
        assert result.is_error is False
        body = json.loads(result.output)
        assert body["timed_out"] is False
        assert body["changes"] == payload["changes"]

    async def test_resume_with_timeout(self):
        from matrix.toolset.workspaces import watch_files_resume

        meta = {"paths": ["a"], "batch_window_ms": 250}
        result = watch_files_resume(meta, YieldTimeout(elapsed_seconds=60.0))
        body = json.loads(result.output)
        assert body["timed_out"] is True
        assert body["changes"] == []
        assert body["elapsed_seconds"] == 60.0

    async def test_resume_with_cancelled(self):
        from matrix.toolset.workspaces import watch_files_resume

        meta = {"paths": ["a"], "batch_window_ms": 250}
        result = watch_files_resume(
            meta,
            YieldCancelled(
                reason="operator skipped",
                cancelled_at=datetime.now(timezone.utc),
                elapsed_seconds=2.0,
            ),
        )
        body = json.loads(result.output)
        assert body["cancelled"] is True
        assert body["reason"] == "operator skipped"
        assert body["changes"] == []


@pytest.mark.asyncio
async def test_watch_files_tool_is_registered():
    tk = build_workspaces_toolset(
        storage_provider=_NoopStorageProvider(),
        workspace_registry=_NoopWorkspaceRegistry(),
    )
    names = {t.id async for t in tk.list_tools()}
    assert "watch_files" in names


def test_watch_files_resume_hook_is_registered():
    import matrix.toolset.workspaces  # noqa: F401
    from matrix.worker.yield_resume_registry import get_resume_hook

    hook = get_resume_hook("watch_files")
    assert callable(hook)
