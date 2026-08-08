"""Transport-level retry for LLM streams.

The split under test: a transport failure (dropped connection, timeout,
5xx, 429) is replayed; an API answering "no" (bad request, unsupported
content, bad credentials) is not, because replaying reproduces the same
rejection and only delays the error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from primer.llm._retry import backoff_delay, is_retryable, stream_with_retry
from primer.model.chat import Done, Error as ChatError, StreamStart, TextDelta
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
    UnsupportedContentError,
)


async def _noop_sleep(_seconds: float) -> None:
    """Collapse backoff so the tests do not actually wait."""
    return None


def _ok_events() -> list:
    return [
        StreamStart(model="m"),
        TextDelta(text="hi", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ]


def _make_source(scripts: list):
    """Build an ``open_stream`` that plays one script per attempt.

    Each entry is either a list of events to yield or an exception to raise.
    """
    attempts = {"n": 0}

    def _open() -> AsyncIterator:
        idx = min(attempts["n"], len(scripts) - 1)
        attempts["n"] += 1
        script = scripts[idx]

        async def _gen():
            if isinstance(script, BaseException):
                raise script
            for ev in script:
                yield ev

        return _gen()

    return _open, attempts


async def _drain(agen) -> list:
    return [e async for e in agen]


def _run(open_stream, *, max_retries=2):
    return stream_with_retry(
        open_stream,
        provider_id="p1",
        provider_kind="anthropic",
        max_retries=max_retries,
        base_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        sleep=_noop_sleep,
    )


class TestClassification:
    @pytest.mark.parametrize("exc", [
        NetworkError("dropped"),
        ProviderTimeoutError("stalled"),
        ServerError("502"),
        RateLimitError("429"),
    ])
    def test_transport_failures_are_retryable(self, exc) -> None:
        assert is_retryable(exc) is True

    @pytest.mark.parametrize("exc", [
        AuthenticationError("401"),
        BadRequestError("400"),
        UnsupportedContentError("no images"),
        ValueError("programmer bug"),
    ])
    def test_api_answers_and_bugs_are_not(self, exc) -> None:
        assert is_retryable(exc) is False


class TestBackoff:
    def test_grows_exponentially_within_the_jitter_ceiling(self) -> None:
        for attempt, ceiling in [(1, 1.0), (2, 2.0), (3, 4.0)]:
            for _ in range(50):
                assert 0.0 <= backoff_delay(attempt, base=1.0, cap=60.0) <= ceiling

    def test_respects_the_cap(self) -> None:
        for _ in range(50):
            assert backoff_delay(10, base=1.0, cap=3.0) <= 3.0


class TestRetryBehaviour:
    async def test_transport_failure_is_replayed(self) -> None:
        open_stream, attempts = _make_source([NetworkError("dropped"), _ok_events()])
        events = await _drain(_run(open_stream))
        assert attempts["n"] == 2
        assert [type(e).__name__ for e in events] == ["StreamStart", "TextDelta", "Done"]

    async def test_api_rejection_is_not_replayed(self) -> None:
        open_stream, attempts = _make_source([BadRequestError("400"), _ok_events()])
        with pytest.raises(BadRequestError):
            await _drain(_run(open_stream))
        assert attempts["n"] == 1, "an explicit rejection must not be retried"

    async def test_gives_up_after_max_retries(self) -> None:
        open_stream, attempts = _make_source([NetworkError("dropped")])
        with pytest.raises(NetworkError):
            await _drain(_run(open_stream, max_retries=2))
        assert attempts["n"] == 3, "initial attempt plus two retries"

    async def test_max_retries_zero_disables_retry(self) -> None:
        open_stream, attempts = _make_source([NetworkError("dropped")])
        with pytest.raises(NetworkError):
            await _drain(_run(open_stream, max_retries=0))
        assert attempts["n"] == 1

    async def test_terminal_error_as_first_event_is_replayed(self) -> None:
        """Nothing escaped yet, so this is still safe to replay."""
        open_stream, attempts = _make_source([
            [ChatError(message="dropped", fatal=True)],
            _ok_events(),
        ])
        events = await _drain(_run(open_stream))
        assert attempts["n"] == 2
        assert [type(e).__name__ for e in events] == ["StreamStart", "TextDelta", "Done"]

    async def test_failure_after_first_event_is_not_replayed(self) -> None:
        """The consumer has already seen output; replaying would duplicate it."""
        open_stream, attempts = _make_source([
            [StreamStart(model="m"), TextDelta(text="par", index=0),
             ChatError(message="dropped", fatal=True)],
            _ok_events(),
        ])
        events = await _drain(_run(open_stream))
        assert attempts["n"] == 1, "committed at the first event"
        assert [type(e).__name__ for e in events] == [
            "StreamStart", "TextDelta", "Error",
        ]

    async def test_mid_stream_raise_is_not_replayed(self) -> None:
        async def _gen():
            yield StreamStart(model="m")
            raise NetworkError("dropped mid-stream")

        attempts = {"n": 0}

        def _open():
            attempts["n"] += 1
            return _gen()

        with pytest.raises(NetworkError):
            await _drain(_run(_open))
        assert attempts["n"] == 1

    async def test_empty_stream_is_forwarded_not_retried(self) -> None:
        open_stream, attempts = _make_source([[]])
        assert await _drain(_run(open_stream)) == []
        assert attempts["n"] == 1
