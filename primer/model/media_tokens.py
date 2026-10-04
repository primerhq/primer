"""One set of token estimates for media blocks, shared by every estimator.

A counter cannot see inside an image, a PDF or an audio clip, so each is a
flat estimate. The character heuristic in ``CompactionStrategy`` and
``char_fallback`` and the native OpenAI-family counter must agree on them: the
native counter used to stand in an ``'x' * 4000`` filler that tokenises to
roughly half the heuristic's figure, so a "native" count came out LOWER than
the estimate it replaced. The module lives in the model layer so the agent
layer can use it without importing the adapters.
"""

from __future__ import annotations

from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    Part,
    VideoPart,
)

IMAGE_TOKENS = 1_000  # Anthropic / OpenAI ballpark
DOCUMENT_TOKENS = 2_000  # PDF page average
AUDIO_VIDEO_TOKENS = 1_500
OTHER_EXTENDED_TOKENS = 500


def media_tokens(part: Part) -> int | None:
    """The flat estimate for a media part, or ``None`` if it is not media."""
    if isinstance(part, ImagePart):
        return IMAGE_TOKENS
    if isinstance(part, DocumentPart):
        return DOCUMENT_TOKENS
    if isinstance(part, ExtendedPart):
        if isinstance(part.extended, (AudioPart, VideoPart)):
            return AUDIO_VIDEO_TOKENS
        return OTHER_EXTENDED_TOKENS
    return None


__all__ = [
    "AUDIO_VIDEO_TOKENS",
    "DOCUMENT_TOKENS",
    "IMAGE_TOKENS",
    "OTHER_EXTENDED_TOKENS",
    "media_tokens",
]
