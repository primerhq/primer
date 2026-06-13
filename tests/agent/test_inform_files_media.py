"""inform_user files -> SessionInformSink resolves workspace files to media."""

from __future__ import annotations

import pytest

from primer.agent.inform import SessionInformSink


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


class _RecordingDispatcher:
    def __init__(self):
        self.envelopes = []

    async def dispatch_prompt(self, *, envelope):
        self.envelopes.append(envelope)
        return [{"ok": True}]


@pytest.mark.asyncio
async def test_inform_attaches_workspace_file_media():
    ws = _FakeWorkspace({"out/report.pdf": b"PDFBYTES"})
    store = _MemArtifacts()
    disp = _RecordingDispatcher()
    sink = SessionInformSink(
        dispatcher=disp, workspace_id="ws-1", session_id="s-1",
        workspace_registry=_FakeWsRegistry(ws), artifact_registry=_ArtReg(store))
    n = await sink("here is your report", files=["out/report.pdf"])
    assert n == 1
    env = disp.envelopes[0]
    assert env.kind == "inform"
    assert env.prompt == "here is your report"
    assert env.media is not None and len(env.media) == 1
    assert env.media[0]["type"] == "document"
    assert env.media[0]["artifact_id"] in store.blobs


@pytest.mark.asyncio
async def test_inform_without_files_has_no_media():
    disp = _RecordingDispatcher()
    sink = SessionInformSink(
        dispatcher=disp, workspace_id="ws-1", session_id="s-1")
    await sink("plain message")
    assert disp.envelopes[0].media is None


@pytest.mark.asyncio
async def test_inform_missing_file_skipped():
    ws = _FakeWorkspace({})  # read_file raises KeyError
    store = _MemArtifacts()
    disp = _RecordingDispatcher()
    sink = SessionInformSink(
        dispatcher=disp, workspace_id="ws-1", session_id="s-1",
        workspace_registry=_FakeWsRegistry(ws), artifact_registry=_ArtReg(store))
    await sink("msg", files=["nope.png"])
    # File unreadable -> no media, but the message still dispatches.
    assert disp.envelopes[0].media is None
