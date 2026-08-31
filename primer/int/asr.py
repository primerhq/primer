"""Abstract base class for speech-to-text (ASR) providers.

Mirrors :mod:`primer.int.llm`: an implementation binds to one configured
provider row at construction time and may serve several models, with the
model chosen per call. There is deliberately no ``list_models`` here --
model and voice discovery is a console concern served by the audio
enumeration passthroughs, not something the runtime needs.

Error contract (the LLM adapters' contract, adapted to a non-streaming
call): implementations MUST NOT raise on provider failure. They RETURN a
:class:`primer.model.speech.SpeechError` instead, so callers can rely on
the coroutine always completing cleanly and can distinguish a retriable
"still warming up" from a hard failure by inspecting ``retriable``.
"""

from abc import ABC, abstractmethod

from primer.model.speech import SpeechError, Transcription


class ASR(ABC):
    """Provider-agnostic speech-to-text interface."""

    @abstractmethod
    async def transcribe(
        self,
        *,
        model: str,
        audio: bytes,
        filename: str,
        mimetype: str,
        language: str | None = None,
    ) -> Transcription | SpeechError:
        """Transcribe one audio payload.

        Parameters
        ----------
        model
            Provider-side transcription model identifier.
        audio
            The complete audio payload. Callers segment long recordings
            before calling; this interface takes one segment.
        filename, mimetype
            Carried because the wire format is multipart and the file
            part MUST be sent as a ``(filename, fileobj, mimetype)``
            triple: a bare handle sends no filename and some servers
            reject it or guess the container wrong.
        language
            Optional BCP-47-ish language hint passed through to the
            provider when set.

        Returns
        -------
        Transcription | SpeechError
            Never raises for provider-side failure.
        """

    async def aclose(self) -> None:
        """Release backend resources. Default no-op; implementations
        holding an HTTP pool MUST override. Idempotent."""
        return


__all__ = ["ASR"]
