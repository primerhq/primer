"""L5: a prompt's artifacts are fetched at once, each distinct one a single time."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from primer.media.hydrate import hydrate_prompt_parts
from primer.model.chat import ImagePart, Message, TextPart


class _Arts:
    def __init__(self):
        self.gets: list[str] = []
        self.active = 0
        self.peak = 0

    async def get(self, aid):
        self.gets.append(aid)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return None if aid == "missing" else SimpleNamespace(data=aid.encode(), mime_type="image/png")


async def test_distinct_artifacts_are_fetched_concurrently_and_a_repeated_one_once():
    arts = _Arts()
    messages = [
        Message(role="user", parts=[TextPart(text="a"), ImagePart(artifact_id="a1", mime_type="image/png"), ImagePart(artifact_id="a2", mime_type="image/png")]),
        Message(role="user", parts=[ImagePart(artifact_id="a1", mime_type="image/png"), ImagePart(artifact_id="a3", mime_type="image/png")]),
    ]
    out = await hydrate_prompt_parts(arts, messages)
    assert sorted(arts.gets) == ["a1", "a2", "a3"] and arts.peak == 3  # a1 once, all three overlapped
    assert [p.data for p in out[0].parts[1:]] == [b"a1", b"a2"] and [p.data for p in out[1].parts] == [b"a1", b"a3"]
    assert all(p.artifact_id is None for m in out for p in m.parts if hasattr(p, "data"))


async def test_a_missing_artifact_becomes_a_note_and_the_rest_are_filled():
    arts = _Arts()
    out = await hydrate_prompt_parts(arts, [Message(role="user", parts=[
        ImagePart(artifact_id="missing", mime_type="image/png"), ImagePart(artifact_id="ok", mime_type="image/png"),
    ])])
    assert out[0].parts[0] == TextPart(text="[attachment unavailable: image/png]")   # GX-8: never an image with no source
    assert out[0].parts[1].data == b"ok"
