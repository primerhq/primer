"""channel/media.py: mime mapping, compression, limits, store/collect/hydrate."""

from __future__ import annotations

import io

import pytest

from primer.channel.media import (
    MediaConfig, MediaTooLarge, MediaTypeNotAllowed, collect_media_parts,
    compress_image, enforce_limits, hydrate_part, hydrate_prompt_parts,
    part_cls_for_mime, store_inbound_media,
)
from primer.int.artifact_storage import ArtifactBlob
from primer.model.chat import AudioPart, DocumentPart, ImagePart, Message, TextPart


class _MemArtifacts:
    def __init__(self):
        self.blobs = {}
        self._n = 0

    async def initialize(self): ...
    async def aclose(self): ...

    async def put(self, *, data, mime_type, filename=None):
        self._n += 1
        aid = f"artifact-{self._n}"
        self.blobs[aid] = ArtifactBlob(data=data, mime_type=mime_type, filename=filename)
        return aid

    async def get(self, artifact_id):
        return self.blobs.get(artifact_id)

    async def delete(self, artifact_id):
        self.blobs.pop(artifact_id, None)


def _png_bytes(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (123, 50, 200)).save(buf, format="PNG")
    return buf.getvalue()


def test_part_cls_for_mime():
    assert part_cls_for_mime("image/png") is ImagePart
    assert part_cls_for_mime("audio/ogg") is AudioPart
    assert part_cls_for_mime("application/pdf") is DocumentPart
    assert part_cls_for_mime(None) is DocumentPart


def test_compress_image_downscales():
    big = _png_bytes(4000, 3000)
    out, mime = compress_image(big, "image/png", MediaConfig(image_max_dimension=2048))
    from PIL import Image
    with Image.open(io.BytesIO(out)) as img:
        assert max(img.size) == 2048
    assert mime == "image/jpeg"
    assert len(out) < len(big)


def test_compress_image_best_effort_on_nonimage():
    data = b"not an image"
    out, mime = compress_image(data, "application/pdf", MediaConfig())
    assert out == data


def test_enforce_limits():
    cfg = MediaConfig(max_bytes=10)
    with pytest.raises(MediaTooLarge):
        enforce_limits(size=11, mime_type="image/png", config=cfg)
    with pytest.raises(MediaTypeNotAllowed):
        enforce_limits(size=1, mime_type="application/x-evil", config=MediaConfig())


@pytest.mark.asyncio
async def test_store_inbound_media_builds_part_and_stores():
    arts = _MemArtifacts()
    part = await store_inbound_media(
        arts, data=_png_bytes(100, 100), mime_type="image/png", filename="a.png")
    assert isinstance(part, ImagePart)
    assert part.artifact_id in arts.blobs
    assert part.data is None  # bytes are in the store, not inline


@pytest.mark.asyncio
async def test_hydrate_part_fills_data():
    arts = _MemArtifacts()
    aid = await arts.put(data=b"PDFBYTES", mime_type="application/pdf")
    part = DocumentPart(artifact_id=aid, mime_type="application/pdf")
    hydrated = await hydrate_part(arts, part)
    assert hydrated.data == b"PDFBYTES"
    assert hydrated.artifact_id is None


def test_collect_media_parts():
    parts = [TextPart(text="hi"), ImagePart(artifact_id="artifact-1", mime_type="image/png")]
    media = collect_media_parts(parts)
    assert len(media) == 1
    assert isinstance(media[0], ImagePart)


@pytest.mark.asyncio
async def test_hydrate_prompt_parts_fills_artifact_backed_parts():
    arts = _MemArtifacts()
    aid = await arts.put(data=b"PNGBYTES", mime_type="image/png")
    messages = [
        Message(role="system", parts=[TextPart(text="sys")]),
        Message(role="user", parts=[
            TextPart(text="look at this"),
            ImagePart(artifact_id=aid, mime_type="image/png"),
        ]),
    ]
    out = await hydrate_prompt_parts(arts, messages)
    assert out[0] is messages[0]  # no binary parts -> same object, no copy
    hydrated_image = out[1].parts[1]
    assert hydrated_image.data == b"PNGBYTES"
    assert hydrated_image.artifact_id is None
    # Original messages/parts are untouched (hydrate_part copies).
    assert messages[1].parts[1].data is None
    assert messages[1].parts[1].artifact_id == aid


@pytest.mark.asyncio
async def test_hydrate_prompt_parts_noop_when_storage_is_none():
    messages = [Message(role="user", parts=[
        ImagePart(artifact_id="art-1", mime_type="image/png"),
    ])]
    out = await hydrate_prompt_parts(None, messages)
    assert out is messages


@pytest.mark.asyncio
async def test_hydrate_prompt_parts_missing_artifact_leaves_part_unhydrated():
    arts = _MemArtifacts()
    messages = [Message(role="user", parts=[
        ImagePart(artifact_id="does-not-exist", mime_type="image/png"),
    ])]
    out = await hydrate_prompt_parts(arts, messages)
    assert out[0].parts[0].artifact_id == "does-not-exist"
    assert out[0].parts[0].data is None
