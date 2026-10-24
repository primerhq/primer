"""``GET /canvas/sessions/{session_id}/files/{token}``: a workspace file the canvas shows, by capability URL (pivot P5, Task 14; Decision 12).

The frame is a sandboxed, cross-origin document, so it sends no cookies: the URL is the credential. The lead's ruling 6 on S6: the URL is
unguessable (signed), scoped to ONE session's workspace file (the session in the path is the token's, and the file is one an Image or Audio
element of that session's canvas references), expires (900 s), and never serves a path outside the workspace or inside its reserved trees.
Every refusal is the same 404, so a refusal tells a caller nothing about which check failed. The workspace here is a real
``LocalWorkspace`` on a temporary directory, so traversal and symlinks are resolved as production resolves them.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.canvas import files as canvas_files
from primer.canvas.files import FILE_URL_TTL_S, canvas_file_secret, mint_file_url
from primer.model.canvas import SessionCanvas
from primer.model.workspace import WorkspaceTemplate
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.workspace.local.workspace import LocalWorkspace

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
OGG = b"OggS" + b"\x00" * 16


def _local(root: Path) -> LocalWorkspace:
    template = WorkspaceTemplate.model_validate({"id": "tpl", "description": "", "provider_id": "p", "backend": {"kind": "local"}})
    return LocalWorkspace(
        workspace_id="ws-1", root=root, template=template, env={},
        state_repo=None, truncation_store=None, tools=[],  # type: ignore[arg-type]
    )


def _image(src: str) -> dict:
    return {"type": "Image", "props": {"src": src, "alt": "x"}}


@pytest.fixture
async def world(app, tmp_path, fake_storage_provider):
    """Workspace ``ws-1`` (a real local directory) with sessions s1 and s2, each with a canvas. The rows are written directly, so a test
    can put a source on a canvas that the batch validator would refuse (a reserved or escaping path): the route must refuse it alone."""
    root = tmp_path / "ws"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "c.png").write_bytes(PNG)
    (root / "assets" / "d.png").write_bytes(PNG)
    (root / "music").mkdir()
    (root / "music" / "song.ogg").write_bytes(OGG)
    (root / "assets" / "dir.png").mkdir()
    (root / ".state" / "sessions" / "s1").mkdir(parents=True)
    (root / ".state" / "sessions" / "s1" / "x.png").write_bytes(b"state")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"outside the workspace")
    (root / "out").symlink_to(outside)
    (root / "state-link.png").symlink_to(root / ".state" / "sessions" / "s1" / "x.png")
    ws = _local(root)

    async def get_workspace(workspace_id: str):
        if workspace_id != "ws-1":
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
        return ws

    app.state.workspace_registry.get_workspace = get_workspace  # type: ignore[method-assign]
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    canvases = fake_storage_provider.get_storage(SessionCanvas)
    s1_sources = [
        "file:assets/c.png", "file:music/song.ogg", "file:assets/missing.png", "file:assets/dir.png", "file:../outside/secret.png",
        "file:out/secret.png", "file:.state/sessions/s1/x.png", "file:state-link.png", "file:notes.svg",
    ]
    for sid, sources in (("s1", s1_sources), ("s2", ["file:assets/d.png"])):
        await sessions.create(WorkspaceSession(
            id=sid, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.WAITING,
            created_at=datetime.now(UTC),
        ))
        ids = [f"e{n}" for n in range(len(sources))]
        elements = {"root": {"type": "Column", "props": {}, "children": ids}}
        for eid, src in zip(ids, sources, strict=True):
            elements[eid] = (
                {"type": "Audio", "props": {"src": src}} if src.endswith(".ogg") else _image(src)
            )
        await canvases.create(SessionCanvas(id=sid, workspace_id="ws-1", rev=1, elements=elements))
    return app


def _url(app, session_id: str, path: str) -> str:
    return mint_file_url(canvas_file_secret(app.state), session_id, path)


async def test_a_referenced_file_is_served_cross_origin(world, raw_client):
    r = await raw_client.get(_url(world, "s1", "assets/c.png"))
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cross-origin-resource-policy"] == "cross-origin"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["cache-control"] == "private, max-age=900"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_an_audio_file_is_served_with_its_type(world, raw_client):
    r = await raw_client.get(_url(world, "s1", "music/song.ogg"))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/ogg" and r.content == OGG


async def test_the_url_needs_no_session_cookie(world, raw_client):
    assert "cookie" not in raw_client.headers and not raw_client.cookies
    assert (await raw_client.get(_url(world, "s1", "assets/c.png"))).status_code == 200


def _tampered(app) -> str:
    url = _url(app, "s1", "assets/c.png")
    head, token = url.rsplit("/", 1)
    return f"{head}/{token[:-2]}{'AA' if not token.endswith('AA') else 'BB'}"


#: case -> the URL it asks for. Every one is a 404, the same 404.
CASES = {
    "tampered": _tampered,
    "made up": lambda app: "/canvas/sessions/s1/files/not-a-token",
    "another session's token": lambda app: _url(app, "s2", "assets/d.png").replace("/sessions/s2/", "/sessions/s1/"),
    "a token for a file the other session shows": lambda app: _url(app, "s1", "assets/d.png"),
    "unreferenced": lambda app: _url(app, "s1", "assets/unlisted.png"),
    "missing file": lambda app: _url(app, "s1", "assets/missing.png"),
    "a directory": lambda app: _url(app, "s1", "assets/dir.png"),
    "traversal": lambda app: _url(app, "s1", "../outside/secret.png"),
    "a symlink out of the workspace": lambda app: _url(app, "s1", "out/secret.png"),
    "the reserved state tree": lambda app: _url(app, "s1", ".state/sessions/s1/x.png"),
    "a symlink into the state tree": lambda app: _url(app, "s1", "state-link.png"),
    "a type that is not media": lambda app: _url(app, "s1", "notes.svg"),
    "a session without a canvas": lambda app: _url(app, "s3", "assets/c.png"),
}


@pytest.mark.parametrize("case", list(CASES))
async def test_refusals_are_404(world, raw_client, case):
    r = await raw_client.get(CASES[case](world))
    assert r.status_code == 404, (case, r.text)
    assert r.json()["detail"] == "This canvas file link is not valid, or it has expired."
    assert b"outside the workspace" not in r.content and b"state" != r.content


async def test_an_expired_url_is_404(world, raw_client, monkeypatch):
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - FILE_URL_TTL_S - 5)
    url = _url(world, "s1", "assets/c.png")
    monkeypatch.setattr(time, "time", real)
    r = await raw_client.get(url)
    assert r.status_code == 404 and r.json()["detail"] == "This canvas file link is not valid, or it has expired."


async def test_a_file_the_canvas_stopped_showing_is_404(world, raw_client, fake_storage_provider):
    url = _url(world, "s1", "assets/c.png")
    assert (await raw_client.get(url)).status_code == 200
    canvases = fake_storage_provider.get_storage(SessionCanvas)
    row = await canvases.get("s1")
    await canvases.update(row.model_copy(update={"elements": {"root": {"type": "Column", "props": {}, "children": []}}}))
    assert (await raw_client.get(url)).status_code == 404


async def test_a_deleted_session_is_404(world, raw_client, fake_storage_provider):
    url = _url(world, "s1", "assets/c.png")
    await fake_storage_provider.get_storage(WorkspaceSession).delete("s1")
    assert (await raw_client.get(url)).status_code == 404


async def test_an_oversized_file_is_413(world, raw_client, monkeypatch):
    monkeypatch.setattr(canvas_files, "MAX_FILE_BYTES", len(PNG) - 1)
    r = await raw_client.get(_url(world, "s1", "assets/c.png"))
    assert r.status_code == 413
