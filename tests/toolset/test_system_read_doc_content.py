"""Unit tests for the workspace-only ``read_doc_content`` system tool.

Covers the handler produced by
:func:`primer.toolset._system_tools.make_read_doc_content_handler`:

* happy path: bytes read from the workspace -> Docling -> ``{text}``;
* the GUARDED optional-``docling`` import degrades to an install hint
  when the extra is not installed (simulated by monkeypatching the
  loaders package so the import raises ModuleNotFoundError) - so these
  tests never require ``docling`` to be installed;
* missing-file / bad-path and no-workspace error branches;
* a document parse failure surfaced in-band.
"""

from __future__ import annotations

import json

import pytest

import primer.ingest.loaders as loaders_mod
from primer.model.except_ import (
    BadRequestError,
    NotFoundError,
    UnsupportedContentError,
)
from primer.model.yield_ import ToolContext
from primer.toolset._system_tools import make_read_doc_content_handler


# ===========================================================================
# Fakes
# ===========================================================================


class _FakeWorkspace:
    def __init__(
        self, *, data: bytes = b"raw-bytes", read_error: Exception | None = None
    ) -> None:
        self._data = data
        self._read_error = read_error
        self.reads: list[str] = []

    async def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        if self._read_error is not None:
            raise self._read_error
        return self._data


class _FakeWorkspaceRegistry:
    def __init__(
        self,
        *,
        workspace: _FakeWorkspace | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.workspace = workspace or _FakeWorkspace()
        self._get_error = get_error
        self.get_calls: list[str] = []

    async def get_workspace(self, workspace_id: str):
        self.get_calls.append(workspace_id)
        if self._get_error is not None:
            raise self._get_error
        return self.workspace


class _FakeLoaded:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeDoclingLoader:
    """Stand-in for the real DoclingLoader; no docling install needed."""

    load_error: Exception | None = None

    def __init__(self) -> None:
        pass

    async def load(self, raw: bytes):
        if _FakeDoclingLoader.load_error is not None:
            raise _FakeDoclingLoader.load_error
        return _FakeLoaded(f"converted:{raw.decode()}")


def _install_fake_loader(monkeypatch) -> None:
    """Route ``from primer.ingest.loaders import DoclingLoader`` to the fake.

    The loaders package resolves ``DoclingLoader`` lazily through a
    module-level ``__getattr__`` (PEP 562), so patching that hook is
    enough - the real docling stack is never imported.
    """
    _FakeDoclingLoader.load_error = None

    def _getattr(name: str):
        if name == "DoclingLoader":
            return _FakeDoclingLoader
        raise AttributeError(name)

    monkeypatch.setattr(loaders_mod, "__getattr__", _getattr, raising=False)


def _install_missing_docling(monkeypatch) -> None:
    """Simulate the ``docling`` extra not being installed."""

    def _getattr(name: str):
        if name == "DoclingLoader":
            raise ModuleNotFoundError("No module named 'docling'")
        raise AttributeError(name)

    monkeypatch.setattr(loaders_mod, "__getattr__", _getattr, raising=False)


def _ctx(workspace_id: str | None = "ws-1") -> ToolContext:
    return ToolContext(
        tool_call_id="tc-1", session_id="sess-1", workspace_id=workspace_id
    )


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_returns_converted_text(monkeypatch) -> None:
    _install_fake_loader(monkeypatch)
    reg = _FakeWorkspaceRegistry(workspace=_FakeWorkspace(data=b"hello"))
    handler = make_read_doc_content_handler(workspace_registry=reg)

    result = await handler({"path": "reports/q3.pdf"}, ctx=_ctx())

    assert not result.is_error
    assert json.loads(result.output) == {"text": "converted:hello"}
    assert reg.get_calls == ["ws-1"]
    assert reg.workspace.reads == ["reports/q3.pdf"]


@pytest.mark.asyncio
async def test_no_workspace_in_ctx_is_error(monkeypatch) -> None:
    _install_fake_loader(monkeypatch)
    reg = _FakeWorkspaceRegistry()
    handler = make_read_doc_content_handler(workspace_registry=reg)

    result = await handler({"path": "a.pdf"}, ctx=_ctx(workspace_id=None))

    assert result.is_error is True
    payload = json.loads(result.output)
    assert payload["type"] == "bad-request"
    assert "workspace" in payload["message"]
    # The workspace was never resolved.
    assert reg.get_calls == []


@pytest.mark.asyncio
async def test_missing_file_is_error(monkeypatch) -> None:
    _install_fake_loader(monkeypatch)
    ws = _FakeWorkspace(read_error=NotFoundError("'a.pdf' not found"))
    reg = _FakeWorkspaceRegistry(workspace=ws)
    handler = make_read_doc_content_handler(workspace_registry=reg)

    result = await handler({"path": "a.pdf"}, ctx=_ctx())

    assert result.is_error is True
    assert json.loads(result.output)["type"] == "not-found"


@pytest.mark.asyncio
async def test_bad_path_is_error(monkeypatch) -> None:
    _install_fake_loader(monkeypatch)
    ws = _FakeWorkspace(read_error=BadRequestError("path escapes workspace"))
    reg = _FakeWorkspaceRegistry(workspace=ws)
    handler = make_read_doc_content_handler(workspace_registry=reg)

    result = await handler({"path": "../escape"}, ctx=_ctx())

    assert result.is_error is True
    assert json.loads(result.output)["type"] == "not-found"


@pytest.mark.asyncio
async def test_missing_docling_extra_returns_install_hint(monkeypatch) -> None:
    # The optional import must be GUARDED: no crash, just a clear hint.
    _install_missing_docling(monkeypatch)
    reg = _FakeWorkspaceRegistry(workspace=_FakeWorkspace(data=b"hello"))
    handler = make_read_doc_content_handler(workspace_registry=reg)

    result = await handler({"path": "a.pdf"}, ctx=_ctx())

    assert result.is_error is True
    payload = json.loads(result.output)
    assert payload["type"] == "unavailable"
    assert "docling" in payload["message"]
    # The file was read before the import was attempted.
    assert reg.workspace.reads == ["a.pdf"]


@pytest.mark.asyncio
async def test_parse_failure_is_in_band_error(monkeypatch) -> None:
    _install_fake_loader(monkeypatch)
    _FakeDoclingLoader.load_error = UnsupportedContentError("cannot parse")
    reg = _FakeWorkspaceRegistry(workspace=_FakeWorkspace(data=b"hello"))
    handler = make_read_doc_content_handler(workspace_registry=reg)

    result = await handler({"path": "a.pdf"}, ctx=_ctx())

    assert result.is_error is True
    payload = json.loads(result.output)
    assert payload["type"] == "bad-request"
    assert "parse" in payload["message"]


@pytest.mark.asyncio
async def test_invalid_arguments_is_error(monkeypatch) -> None:
    _install_fake_loader(monkeypatch)
    reg = _FakeWorkspaceRegistry()
    handler = make_read_doc_content_handler(workspace_registry=reg)

    # Empty path violates min_length=1.
    result = await handler({"path": ""}, ctx=_ctx())

    assert result.is_error is True
