"""The one place that turns a token count into a labelled number, never an error.

Counters (``LLM.count_tokens_detailed``) raise: a typed
``TokenCounterUnavailable``, or a mapped provider error. This wrapper is what
a turn path calls. It

* never raises (``CancelledError`` excepted) and never blocks past a backstop,
* falls back to an estimate and says so (``source="estimate"`` plus an
  ``outcome`` naming why), so an estimate is never presented as a count,
* labels what a successful count stands on (``native`` only when the
  counter is the model's own tokenizer or a vendor endpoint, ``native_approx``
  for another family's tokenizer, ``native_plus_estimated`` when parts of the
  prompt were estimated rather than counted),
* negative-caches TRANSIENT failures for a short TTL per ``(provider, model)``
  so a down endpoint is not retried, or logged, on every turn. Deterministic
  rejections (a 400 caused by one conversation's content) are never cached:
  the payload is the thing to fix, and caching per model would spread one
  conversation's failure to every session,
* treats anything unexpected as a bug: ``outcome="fallback_bug"``, an ERROR
  with the traceback, and a counter the test suite asserts stays at zero. A
  catch-all that quietly estimated would hide a programming error behind a
  plausible number.

The internal char-fallbacks that used to live inside individual adapters hid
exactly that; the wrapper alone owns fallback and labelling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import primer.observability.metrics as _metrics
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import Message, Tool
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
    TokenCounterUnavailable,
    TransientError,
    UnsupportedContentError,
)
from primer.model.token_count import TokenCount

if TYPE_CHECKING:
    from primer.int.llm import LLM


logger = logging.getLogger(__name__)

CountSource = Literal["native", "native_approx", "native_plus_estimated", "estimate"]
CountOutcome = Literal[
    "ok",
    "fallback_timeout",
    "fallback_unavailable",
    "fallback_transient",
    "fallback_rejected",
    "negative_cached",
    "no_counter",
    "fallback_bug",
]

# Failures a retry may cure: cached for NEGATIVE_TTL_S.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    NetworkError,
    ProviderTimeoutError,
    ServerError,
    RateLimitError,
    TransientError,
)
# Failures the request itself causes: reported, never cached.
REJECTED_ERRORS: tuple[type[BaseException], ...] = (
    BadRequestError,
    AuthenticationError,
    UnsupportedContentError,
)

NEGATIVE_TTL_S = 60.0
# Defence in depth above the per-call SDK timeouts and the executor queue wait.
DEFAULT_BACKSTOP_S = 10.0


@dataclass(frozen=True)
class CountResult:
    total: int
    source: CountSource
    outcome: CountOutcome
    estimated_components: tuple[str, ...] = ()
    reason: str | None = None


class NegativeCache:
    """Per ``(provider, model)`` TTL cache of transient counter failures."""

    def __init__(
        self,
        *,
        ttl_s: float = NEGATIVE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._until: dict[tuple[str, str], float] = {}

    @property
    def ttl_s(self) -> float:
        return self._ttl

    def active(self, key: tuple[str, str]) -> bool:
        until = self._until.get(key)
        if until is None:
            return False
        if self._clock() >= until:
            del self._until[key]
            return False
        return True

    def add(self, key: tuple[str, str]) -> None:
        self._until[key] = self._clock() + self._ttl

    def clear(self) -> None:
        self._until.clear()


DEFAULT_NEGATIVE_CACHE = NegativeCache()

_fallback_bug_count = 0


def fallback_bug_count() -> int:
    """How many unexpected counter failures this process has swallowed.

    The test suite asserts it does not move (tests/conftest.py): a counter bug
    must fail a test, not turn into a plausible-looking estimate.
    """
    return _fallback_bug_count


def _default_estimate(messages: Sequence[Message], tools: Sequence[Tool] | None) -> int:
    return count_tokens_char_fallback(messages=messages, tools=tools)


def _source_of(detail: TokenCount) -> CountSource:
    if detail.estimated_components:
        return "native_plus_estimated"
    return "native" if detail.exact else "native_approx"


def _record(provider: str, source: str, outcome: str, started: float) -> None:
    _metrics.llm_count_tokens_total.labels(provider, source, outcome).inc()
    _metrics.llm_count_tokens_seconds.labels(provider, source).observe(
        time.monotonic() - started,
    )


async def count_prompt_tokens(
    llm: "LLM | Any",
    *,
    model: Any,
    messages: Sequence[Message],
    tools: Sequence[Tool] | None = None,
    estimate: Callable[[Sequence[Message], Sequence[Tool] | None], int] = _default_estimate,
    backstop_s: float = DEFAULT_BACKSTOP_S,
    negative_cache: NegativeCache = DEFAULT_NEGATIVE_CACHE,
) -> CountResult:
    """Count ``messages`` (+ ``tools``) natively, or estimate and say why.

    ``model`` is a ``ResolvedModel`` (anything with ``provider_id``,
    ``profile_id`` and ``model_name``). The system prompt is passed as a
    system-role message in ``messages``; the counter decides what to do with it.
    """
    global _fallback_bug_count  # noqa: PLW0603

    provider = model.provider_id or model.profile_id
    model_name = model.model_name or model.profile_id
    key = (provider, model_name)
    started = time.monotonic()

    def fallback(outcome: CountOutcome, reason: str) -> CountResult:
        _record(provider, "estimate", outcome, started)
        return CountResult(
            total=estimate(messages, tools), source="estimate",
            outcome=outcome, reason=reason,
        )

    detailed = getattr(llm, "count_tokens_detailed", None)
    legacy = getattr(llm, "count_tokens", None)
    if detailed is None and legacy is None:
        return fallback("no_counter", f"{type(llm).__name__} has no counter")

    async def count() -> TokenCount:
        call = dict(
            model=model_name, messages=list(messages),
            tools=list(tools) if tools else None,
        )
        if detailed is not None:
            return await detailed(**call)
        return TokenCount(total=await legacy(**call), exact=False)

    if negative_cache.active(key):
        return fallback("negative_cached", "a recent transient failure is cached")

    try:
        detail = await asyncio.wait_for(count(), backstop_s)
    except TokenCounterUnavailable as exc:
        if exc.transient:
            _remember(negative_cache, key, provider, model_name, exc)
        else:
            logger.debug("token counter unavailable for %s/%s: %s", provider, model_name, exc)
        return fallback("fallback_unavailable", exc.message)
    except TRANSIENT_ERRORS as exc:
        _remember(negative_cache, key, provider, model_name, exc)
        outcome: CountOutcome = (
            "fallback_timeout" if isinstance(exc, (TimeoutError, ProviderTimeoutError))
            else "fallback_transient"
        )
        return fallback(outcome, f"{type(exc).__name__}: {exc}")
    except REJECTED_ERRORS as exc:
        logger.warning(
            "token count rejected by %s for model %s (%s); estimating this prompt",
            provider, model_name, type(exc).__name__,
        )
        return fallback("fallback_rejected", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - deliberate: unexpected is a bug, never a count
        _fallback_bug_count += 1
        logger.error(
            "token counter for %s/%s raised an unexpected %s; this is a bug, "
            "estimating this prompt",
            provider, model_name, type(exc).__name__, exc_info=True,
        )
        return fallback("fallback_bug", f"{type(exc).__name__}: {exc}")

    source = _source_of(detail)
    _record(provider, source, "ok", started)
    return CountResult(
        total=detail.total,
        source=source,
        outcome="ok",
        estimated_components=tuple(detail.estimated_components),
    )


def _remember(
    cache: NegativeCache,
    key: tuple[str, str],
    provider: str,
    model_name: str,
    exc: BaseException,
) -> None:
    already = cache.active(key)
    cache.add(key)
    if not already:
        logger.warning(
            "token counter for %s/%s failed transiently (%s: %s); estimating for the "
            "next %.0fs without retrying",
            provider, model_name, type(exc).__name__, exc, cache.ttl_s,
        )


__all__ = [
    "DEFAULT_BACKSTOP_S",
    "DEFAULT_NEGATIVE_CACHE",
    "NEGATIVE_TTL_S",
    "REJECTED_ERRORS",
    "TRANSIENT_ERRORS",
    "CountOutcome",
    "CountResult",
    "CountSource",
    "NegativeCache",
    "count_prompt_tokens",
    "fallback_bug_count",
]
