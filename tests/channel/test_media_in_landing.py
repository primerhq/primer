"""S6 P4: inbound attachments become workspace files.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 6 - media lands
under media/<fire_id>/<filename> and the steer text references the paths.
"""

from __future__ import annotations

from primer.channel.media_in import compose_steer_text, land_media_in_workspace


class _Blob:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.mime_type = "audio/ogg"


class _FakeArtifacts:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    async def get(self, artifact_id: str):
        raw = self._blobs.get(artifact_id)
        return _Blob(raw) if raw is not None else None


class _FakeWorkspace:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path: str, content: bytes) -> None:
        self.files[path] = content


class _Part:
    def __init__(self, artifact_id=None, data=None, filename=None) -> None:
        self.artifact_id = artifact_id
        self.data = data
        self.filename = filename


async def test_artifact_backed_part_is_written_under_the_fire_inbox():
    ws = _FakeWorkspace()
    paths = await land_media_in_workspace(
        workspace=ws, fire_id="fire-1",
        parts=[_Part(artifact_id="a1", filename="note.ogg")],
        artifact_storage=_FakeArtifacts({"a1": b"OggS"}),
    )
    assert paths == ["media/fire-1/note.ogg"]
    assert ws.files["media/fire-1/note.ogg"] == b"OggS"


async def test_inline_part_needs_no_artifact_store():
    ws = _FakeWorkspace()
    paths = await land_media_in_workspace(
        workspace=ws, fire_id="fire-2",
        parts=[_Part(data=b"PNG", filename="shot.png")],
        artifact_storage=None,
    )
    assert paths == ["media/fire-2/shot.png"]


async def test_unnamed_and_escaping_filenames_are_made_safe():
    ws = _FakeWorkspace()
    paths = await land_media_in_workspace(
        workspace=ws, fire_id="fire-3",
        parts=[_Part(data=b"x", filename="../../etc/passwd"), _Part(data=b"y")],
        artifact_storage=None,
    )
    assert paths == ["media/fire-3/passwd", "media/fire-3/attachment-1"]


async def test_unresolvable_part_is_skipped():
    ws = _FakeWorkspace()
    paths = await land_media_in_workspace(
        workspace=ws, fire_id="fire-4",
        parts=[_Part(artifact_id="missing", filename="gone.bin")],
        artifact_storage=_FakeArtifacts({}),
    )
    assert paths == []
    assert ws.files == {}


def test_steer_text_references_the_paths():
    out = compose_steer_text("look at this", ["media/f/a.png"])
    assert "look at this" in out
    assert "media/f/a.png" in out


def test_transcript_replaces_the_caption_and_keeps_the_paths():
    out = compose_steer_text(
        "", ["media/f/n.ogg"], transcript="hello there",
    )
    assert out.startswith("hello there")
    assert "media/f/n.ogg" in out


def test_note_is_appended_when_present():
    out = compose_steer_text("hi", ["media/f/n.ogg"], note="no STT configured")
    assert "no STT configured" in out
