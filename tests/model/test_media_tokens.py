"""One set of media token estimates, shared by every estimator."""

from __future__ import annotations

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.llm._tokenizer.char_fallback import _estimate_part
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    TextPart,
    VideoPart,
)
from primer.model.media_tokens import (
    AUDIO_VIDEO_TOKENS,
    DOCUMENT_TOKENS,
    IMAGE_TOKENS,
    OTHER_EXTENDED_TOKENS,
    media_tokens,
)


def test_the_constants_are_the_historical_heuristic_values():
    assert (IMAGE_TOKENS, DOCUMENT_TOKENS, AUDIO_VIDEO_TOKENS, OTHER_EXTENDED_TOKENS) == (
        1_000, 2_000, 1_500, 500,
    )


def test_text_is_not_media():
    assert media_tokens(TextPart(text="hi")) is None


@pytest.mark.parametrize(
    "part",
    [
        ImagePart(mime_type="image/png", data=b"\x00"),
        DocumentPart(mime_type="application/pdf", data=b"%PDF"),
        ExtendedPart(extended=AudioPart(mime_type="audio/wav", data=b"\x00")),
        ExtendedPart(extended=VideoPart(mime_type="video/mp4", data=b"\x00")),
    ],
)
def test_every_estimator_agrees_with_the_shared_constants(part):
    expected = media_tokens(part)
    assert expected is not None
    assert CompactionStrategy._estimate_part_tokens(part) == expected
    assert _estimate_part(part) == expected


def test_split_media_removes_media_and_estimates_it_without_touching_the_input():
    from primer.model.chat import Message
    from primer.model.media_tokens import split_media

    text_only = Message(role="user", parts=[TextPart(text="hi")])
    mixed = Message(role="user", parts=[
        TextPart(text="look"), ImagePart(mime_type="image/png", data=b"\x00"),
        DocumentPart(mime_type="application/pdf", data=b"%PDF"),
    ])
    media_only = Message(role="user", parts=[ImagePart(mime_type="image/png", data=b"\x00")])
    before = [m.model_dump() for m in (text_only, mixed, media_only)]

    kept, estimate = split_media([text_only, mixed, media_only])

    assert estimate == IMAGE_TOKENS + DOCUMENT_TOKENS + IMAGE_TOKENS
    assert len(kept) == 2, "a message left with no parts is dropped"
    assert kept[0] is text_only, "an untouched message is passed through as is"
    assert [type(p).__name__ for p in kept[1].parts] == ["TextPart"]
    assert [m.model_dump() for m in (text_only, mixed, media_only)] == before
