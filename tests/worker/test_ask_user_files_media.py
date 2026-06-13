"""ask_user files -> worker resolves workspace files to envelope media."""

from __future__ import annotations

import pytest

from primer.worker.yield_runtime import _dispatch_to_channels, _resolve_files_to_media


class _FakeWorkspace:
    def __init__(self, files):
        self._files = files

    async def read_file(self, path):
        return self._files[path]


class _FakeWsRegistry:
    def __init__(self, ws):
        self._ws = ws

    async def get_workspace(self, workspace_id):
        return self._ws


class _MemArtifacts:
    def __init__(self):
        from primer.int.artifact_storage import ArtifactBlob
        self._cls = ArtifactBlob
        self.blobs = {}
        self._n = 0

    async def put(self, *, data, mime_type, filename=None):
        self._n += 1
        aid = f"artifact-{self._n}"
        self.blobs[aid] = self._cls(data=data, mime_type=mime_type, filename=filename)
        return aid

    async def get(self, aid):
        return self.blobs.get(aid)


class _ArtReg:
    def __init__(self, store):
        self._store = store

    async def get_default(self):
        return self._store


class _Disp:
    def __init__(self):
        self.envelopes = []

    async def dispatch_prompt(self, *, envelope):
        self.envelopes.append(envelope)
        return [{"ok": True}]


class _Yielded:
    tool_name = "ask_user"
    event_key = "ask_user:s-1:tc1"

    def __init__(self, files):
        self.resume_metadata = {"prompt": "approve?", "files": files,
                                "tool_call_id": "tc1"}


class _Session:
    id = "s-1"
    workspace_id = "ws-1"


@pytest.mark.asyncio
async def test_resolve_files_to_media():
    store = _MemArtifacts()
    ws = _FakeWorkspace({"a.txt": b"hello"})
    media = await _resolve_files_to_media(
        workspace_registry=_FakeWsRegistry(ws), artifact_registry=_ArtReg(store),
        workspace_id="ws-1", files=["a.txt"])
    assert media is not None and len(media) == 1
    assert media[0]["artifact_id"] in store.blobs


@pytest.mark.asyncio
async def test_dispatch_attaches_ask_user_media():
    store = _MemArtifacts()
    ws = _FakeWorkspace({"chart.png": b"\x89PNG\r\n"})
    disp = _Disp()
    await _dispatch_to_channels(
        dispatcher=disp, session=_Session(), yielded=_Yielded(["chart.png"]),
        workspace_registry=_FakeWsRegistry(ws), artifact_registry=_ArtReg(store))
    env = disp.envelopes[0]
    assert env.kind == "ask_user"
    assert env.media is not None and len(env.media) == 1


@pytest.mark.asyncio
async def test_dispatch_no_files_no_media():
    disp = _Disp()
    await _dispatch_to_channels(
        dispatcher=disp, session=_Session(), yielded=_Yielded(None))
    assert disp.envelopes[0].media is None


def test_ask_user_handler_carries_files():
    import asyncio
    from primer.model.yield_ import ToolContext, Yielded
    from primer.toolset.misc import _ask_user_handler
    ctx = ToolContext(tool_call_id="tc1", session_id="s-1", workspace_id="ws-1")
    res = asyncio.get_event_loop().run_until_complete(
        _ask_user_handler({"prompt": "q", "files": ["a.png"]}, ctx=ctx))
    assert isinstance(res, Yielded)
    assert res.resume_metadata["files"] == ["a.png"]
