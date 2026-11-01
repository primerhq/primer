"""Map adapter-level exceptions to the SpeechError value type."""

from __future__ import annotations

from primer.common.openai_errors import classify_openai_exception
from primer.llm._retry import is_retryable
from primer.model.speech import SpeechError, WARMING_UP_CODE


def is_replayable(exc: Exception) -> bool:
    """A transport failure (connect, timeout, 429, 5xx) the row's ``max_retries`` replays when nothing was yielded.

    The SDK's own retries are off (W50), so this is the one retry rule; it covers what the SDK retried before.
    """
    return is_retryable(classify_openai_exception(exc))


def speech_error_from_exception(exc: Exception) -> SpeechError:
    """Translate a provider exception into a terminal error value.

    503 is special-cased BEFORE the generic classifier: an ASR service
    returns ``503 model still loading`` for roughly its first ten
    seconds and a vLLM-class service can take minutes, so the honest
    answer is "back off and retry", not "this failed".
    """
    status = getattr(exc, "status_code", None)
    if status == 503:
        return SpeechError(
            code=WARMING_UP_CODE,
            message="provider is still loading its model; retry shortly",
            retriable=True,
        )
    err = classify_openai_exception(exc)
    return SpeechError(
        code=getattr(err, "code", None) or type(exc).__name__,
        message=getattr(err, "message", None) or str(exc),
        retriable=False,
    )


__all__ = ["is_replayable", "speech_error_from_exception"]
