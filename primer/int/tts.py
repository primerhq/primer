"""Abstract base class for text-to-speech (TTS) providers.

Mirrors :mod:`primer.int.llm`, including the streaming shape:
:meth:`TTS.stream` is an async generator so audio reaches the caller AS
IT IS SYNTHESISED. Collecting the chunks and returning the join is the
mistake this interface exists to prevent -- it reintroduces the full
synthesis wait while looking like streaming.

Error contract: implementations MUST NOT raise on provider failure. They
yield a terminal :class:`primer.model.speech.SpeechError` and stop, so a
consumer can always drain the iterator to completion.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from primer.model.speech import SpeechError


class TTS(ABC):
    """Provider-agnostic streaming text-to-speech interface."""

    @abstractmethod
    def stream(
        self,
        *,
        model: str,
        text: str,
        voice: str,
        response_format: str = "mp3",
    ) -> AsyncIterator[bytes | SpeechError]:
        """Stream synthesised audio for ``text``.

        Concrete implementations are async generators
        (``async def stream(...): ... yield chunk``).

        Parameters
        ----------
        model
            Provider-side synthesis model identifier.
        text
            The text to speak.
        voice
            Provider-side voice name. Voice names are not portable
            across providers, so the caller resolves this before the
            call rather than the adapter defaulting it.
        response_format
            Container for the returned audio. ``mp3`` is the default
            because it decodes as a chunked stream in every modern
            ``<audio>`` element; ``pcm`` suits a local device or a
            downstream model.

        Yields
        ------
        bytes
            Audio chunks, in order, as they arrive from the provider.
        SpeechError
            A single terminal error value; nothing follows it.
        """

    async def aclose(self) -> None:
        """Release backend resources. Default no-op; implementations
        holding an HTTP pool MUST override. Idempotent."""
        return


__all__ = ["TTS"]
