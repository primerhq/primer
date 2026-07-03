"""Tests for ExecutionContext + build_execution_context (primer.model.graph)."""

from __future__ import annotations

from primer.model.graph import ExecutionContext, build_execution_context


class TestBuildExecutionContext:
    def test_memory_default_has_null_workspace_fields(self) -> None:
        ctx = build_execution_context()
        assert isinstance(ctx, ExecutionContext)
        assert ctx.surface == "memory"
        assert ctx.workspace_id is None
        assert ctx.session_id is None
        assert ctx.graph_id is None
        assert ctx.artifact_dir is None
        assert ctx.principal is None
        assert ctx.now is not None  # defaulted to an ISO timestamp

    def test_workspace_fields_and_derived_artifact_dir(self) -> None:
        ctx = build_execution_context(
            surface="workspace",
            workspace_id="ws-1",
            session_id="gsid-1",
            graph_id="g-1",
            principal="operator",
            now="2026-07-03T00:00:00+00:00",
        )
        assert ctx.surface == "workspace"
        assert ctx.workspace_id == "ws-1"
        assert ctx.session_id == "gsid-1"
        assert ctx.graph_id == "g-1"
        assert ctx.artifact_dir == "artifacts/gsid-1"
        assert ctx.principal == "operator"
        assert ctx.now == "2026-07-03T00:00:00+00:00"

    def test_nested_session_id_nests_artifact_dir(self) -> None:
        ctx = build_execution_context(
            surface="workspace", session_id="gsid-1__research"
        )
        assert ctx.artifact_dir == "artifacts/gsid-1__research"

    def test_explicit_now_is_passed_through(self) -> None:
        ctx = build_execution_context(now="FIXED")
        assert ctx.now == "FIXED"

    def test_model_is_frozen(self) -> None:
        import pytest
        from pydantic import ValidationError

        ctx = build_execution_context()
        with pytest.raises((ValidationError, TypeError, AttributeError)):
            ctx.surface = "workspace"  # type: ignore[misc]
