"""read_doc_content is UTF-8 text only (v2).

Binary document conversion went with the ingest package: a collection
holds text, and converting a binary is a job for a tool the operator runs
before the file reaches primer.
"""
from __future__ import annotations

import json

import pytest

from primer.api.registries import ProviderRegistry
from primer.model.yield_ import ToolContext
from primer.toolset._system_tools import make_read_doc_content_handler
from primer.toolset.system import build_system_toolset


class _FakeWorkspace:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read_file(self, path: str) -> bytes:
        return self._data


class _FakeWorkspaceRegistry:
    def __init__(self, data: bytes) -> None:
        self.workspace = _FakeWorkspace(data)

    async def get_workspace(self, workspace_id: str):
        return self.workspace


@pytest.mark.asyncio
async def test_utf8_file_returns_text() -> None:
    handler = make_read_doc_content_handler(
        workspace_registry=_FakeWorkspaceRegistry(b"# Title\n\nbody"),
    )
    res = await handler(
        {"path": "notes.md"}, ctx=ToolContext(workspace_id="ws-1", tool_call_id="tc-1", session_id="s-1"),
    )
    assert not res.is_error, res.output
    assert json.loads(res.output)["text"] == "# Title\n\nbody"


@pytest.mark.asyncio
async def test_binary_file_is_a_clear_bad_request() -> None:
    handler = make_read_doc_content_handler(
        workspace_registry=_FakeWorkspaceRegistry(b"\x89PNG\x00\x01\xff"),
    )
    res = await handler(
        {"path": "logo.png"}, ctx=ToolContext(workspace_id="ws-1", tool_call_id="tc-1", session_id="s-1"),
    )
    assert res.is_error
    body = json.loads(res.output)
    assert body["type"] == "bad-request"
    assert "UTF-8 text files only" in body["message"]


def test_the_descriptor_promises_text_only_not_document_conversion(
    fake_storage_provider,
) -> None:
    """The descriptor an agent reads (rendered description plus the argument
    schema) must not repeat the pre-v2 conversion promise: the handler decodes
    UTF-8 and refuses everything else."""
    registry = ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    provider = build_system_toolset(
        storage_provider=fake_storage_provider,
        provider_registry=registry,
        workspace_registry=_FakeWorkspaceRegistry(b"# Title\n\nbody"),
    )
    tool, _ = provider._registry["read_doc_content"]  # noqa: SLF001
    descriptor = tool.description + "\n" + json.dumps(tool.args_schema)
    for stale in ("PDF", "DOCX", "PPTX", "HTML"):
        assert stale not in descriptor
