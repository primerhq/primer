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
