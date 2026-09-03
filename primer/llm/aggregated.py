"""Aggregated chat-model adapter.

Wraps an ordered pool of downstream ModelProfile members (``kind==
"single"``) behind one LLM interface. On a rate-limited / unavailable
member the adapter fails over to the next member. Members are resolved
LAZILY on each ``stream`` call through the ``resolve_member`` callable
(which :func:`primer.model_profile.resolve_llm` binds to itself, so a
member that is itself resolved recursively), so member edits and cache
invalidations are picked up transparently and no build-time cycle is
created.

Two failover channels, mirroring the real adapters:

* Connect phase (before the first event): the member's ``stream``
  RAISES a typed exception (RateLimitError, ServerError, ...). Matched
  by class - the reliable channel.
* Mid-stream (after >= 1 event): the member usually YIELDS a terminal
  ``Error(fatal=True)`` (whose ``code`` is often ``None`` for transient
  errors, so eligibility is best-effort by code - see
  ``_yielded_eligible``), but a mid-stream timeout is RAISED instead
  (ProviderTimeoutError). In ``MID_STREAM`` mode both a yielded fatal
  eligible Error and a raised eligible exception restart on the next
  member (already-shown tokens may duplicate); in ``BEFORE_FIRST_TOKEN``
  mode a post-commit failure is surfaced/propagated (never re-emit).

Ownership: downstream LLMs are owned/cached by the ProviderRegistry.
``AggregatedLLM.aclose`` is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from primer.int.llm import LLM
from primer.model.chat import Error as ChatError
from primer.model.chat import Message, StreamEvent, Tool, ToolChoice
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    NetworkError,
    NotFoundError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
)
from primer.model.model_profile import (
    FailoverClasses,
    FailoverPoint,
    ModelProfile,
    RoutingStrategy,
)

# Deferred: primer.model_profile.resolver (home of ResolvedModel and the
# resolve_llm function that constructs THIS class) imports AggregatedLLM
# at runtime to build an aggregated adapter, so importing ResolvedModel
# back at module scope here would be circular. Type-only under
# TYPE_CHECKING is safe because `from __future__ import annotations`
# above defers every annotation to a string.
if TYPE_CHECKING:
    from primer.model_profile.resolver import ResolvedModel


logger = logging.getLogger(__name__)


# RAISED-exception eligibility (the reliable connect-phase channel).
_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    RateLimitError,
    ServerError,
    ProviderTimeoutError,
    NetworkError,
)
_CONFIG_EXC: tuple[type[BaseException], ...] = (
    AuthenticationError,
    BadRequestError,
)

# YIELDED-error eligibility. DEFENSIVE ONLY: every current adapter RAISES
# its timeouts mid-stream (ProviderTimeoutError - openchat.py:284,
# anthropic.py:760), so these codes never actually arrive on a yielded
# Error today. They are matched here so the policy stays correct if an
# adapter ever starts yielding a timeout instead of raising it. Every
# other transient/server/rate-limit/auth/network error classifies with
# code=None (see primer/common/anthropic_errors.py and primer/llm/ollama.py).
_TRANSIENT_CODES: frozenset[str] = frozenset({
    "stream_timeout",
    "generation_timeout",
    "connect_timeout",
})


def _exc_eligible(exc: BaseException, failover_on: FailoverClasses) -> bool:
    if isinstance(exc, _TRANSIENT_EXC):
        return True
    if failover_on == FailoverClasses.TRANSIENT_AND_CONFIG and isinstance(exc, _CONFIG_EXC):
        return True
    return False


def _yielded_eligible(code: str | None, failover_on: FailoverClasses) -> bool:
    """Best-effort eligibility for a YIELDED fatal Error, keyed on ``code``.

    - a known timeout code -> eligible under either policy.
    - ``None`` -> eligible under either policy. Rate-limit, server, network,
      AND auth all classify with code=None in every classifier family
      (anthropic_errors.py, ollama.py, and the OpenAI-compatible
      classifiers), and a code-less bad-request is itself config-eligible,
      so None is treated as eligible. NOTE: this means the yielded channel
      CANNOT honor the TRANSIENT policy's "exclude auth" guarantee - an auth
      error that surfaced as a yielded fatal Error would fail over even
      under TRANSIENT. This is safe because the yielded-error failover
      window closes before any token is emitted downstream (auth failures
      also RAISE at connect in practice, where the class IS preserved).
    - any other (non-null) code appears only on bad-request (provider code)
      or OpenAI-native mid-stream errors -> config-eligible, i.e. matched
      only under TRANSIENT_AND_CONFIG.
    """
    if code in _TRANSIENT_CODES or code is None:
        return True
    return failover_on == FailoverClasses.TRANSIENT_AND_CONFIG


async def _safe_aclose(agen: AsyncIterator[StreamEvent]) -> None:
    aclose = getattr(agen, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # noqa: BLE001 -- best-effort cleanup of an abandoned stream
        pass


class AggregatedLLM(LLM):
    """Virtual chat model that fails over across an ordered member pool."""

    def __init__(
        self,
        profile: ModelProfile,
        *,
        resolve_member: Callable[[str], Awaitable[tuple[LLM, "ResolvedModel"]]],
    ) -> None:
        assert profile.kind == "aggregated"
        assert profile.members is not None
        self._profile = profile
        self._members: list[str] = profile.members
        self._strategy = profile.strategy
        self._failover_point = profile.failover_point
        self._failover_on = profile.failover_on
        self._resolve = resolve_member
        self._cursor = 0
        self._cursor_lock = asyncio.Lock()

    async def _member_order(self) -> list[str]:
        members = self._members
        if self._strategy == RoutingStrategy.SEQUENTIAL:
            return list(members)
        n = len(members)
        async with self._cursor_lock:
            start = self._cursor
            self._cursor = (self._cursor + 1) % n
        return [members[(start + i) % n] for i in range(n)]

    def _log_failover(self, member_id: str, reason: str) -> None:
        logger.info(
            "aggregated-llm %s: member %s failing over: %s",
            self._profile.id, member_id, reason,
        )

    async def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        stop: list[str] | None = None,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
        tools: list[Tool] | None = None,
        tool_choice: ToolChoice | None = None,
        extended: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        failover_point = self._failover_point
        failover_on = self._failover_on
        errors: list[str] = []
        for member_id in await self._member_order():
            try:
                llm, resolved = await self._resolve(member_id)
            except NotFoundError:
                errors.append(f"{member_id}: not found")
                self._log_failover(member_id, "profile not found")
                continue
            if isinstance(llm, AggregatedLLM):
                raise BadRequestError(
                    f"aggregated model profile {self._profile.id!r} member "
                    f"{member_id!r} resolves to another aggregated "
                    f"profile; nesting/self-reference is not allowed",
                )
            agen = llm.stream(
                model=resolved.model_name,
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
                stop=stop,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                extended=extended,
            )
            # The per-member ``finally`` is the single cleanup point: it
            # acloses ``agen`` on EVERY exit path - failover-continue, a
            # surfaced/propagated exception, normal completion (return), AND
            # consumer abandonment (a GeneratorExit thrown into this async
            # generator at a ``yield`` while ``agen`` is still open). Without
            # it, an abandoned downstream stream would leak its rate-limiter
            # slot (openchat.py:245, anthropic.py:708) - fatal for
            # max_concurrency:1 backends. aclose on an exhausted/already-closed
            # generator is a harmless no-op, so this subsumes per-path aclose.
            failed_over = False
            try:
                # --- connect phase: pull the first event ---
                try:
                    first = await agen.__anext__()
                except StopAsyncIteration:
                    errors.append(f"{member_id}: empty stream")
                    self._log_failover(member_id, "empty stream")
                    continue
                except (ProviderError, NetworkError) as exc:
                    if _exc_eligible(exc, failover_on):
                        errors.append(f"{member_id}: {type(exc).__name__}")
                        self._log_failover(member_id, f"connect {type(exc).__name__}: {exc}")
                        continue
                    raise
                # --- first event in hand ---
                if (
                    isinstance(first, ChatError)
                    and first.fatal
                    and _yielded_eligible(first.code, failover_on)
                ):
                    errors.append(f"{member_id}: first-event Error code={first.code}")
                    self._log_failover(member_id, f"first-event Error code={first.code}")
                    continue
                # commit to this member: nothing has been yielded downstream yet.
                yield first
                # --- stream the rest ---
                try:
                    async for ev in agen:
                        if (
                            failover_point == FailoverPoint.MID_STREAM
                            and isinstance(ev, ChatError)
                            and ev.fatal
                            and _yielded_eligible(ev.code, failover_on)
                        ):
                            errors.append(f"{member_id}: mid-stream Error code={ev.code}")
                            self._log_failover(
                                member_id,
                                f"mid-stream YIELDED Error code={ev.code} (tokens may duplicate)",
                            )
                            failed_over = True
                            break
                        yield ev
                except (ProviderError, NetworkError) as exc:
                    # Mid-stream RAISED failure (e.g. ProviderTimeoutError,
                    # openchat.py:284 / anthropic.py:760). We already committed
                    # tokens, so only MID_STREAM may restart on the next member;
                    # BEFORE_FIRST_TOKEN cannot fail over post-commit -> propagate.
                    if (
                        failover_point == FailoverPoint.MID_STREAM
                        and _exc_eligible(exc, failover_on)
                    ):
                        errors.append(f"{member_id}: mid-stream {type(exc).__name__}")
                        self._log_failover(
                            member_id,
                            f"mid-stream RAISED {type(exc).__name__} (tokens may duplicate)",
                        )
                        failed_over = True
                    else:
                        raise
                if failed_over:
                    continue
                return  # stream completed on this member (success or surfaced error)
            finally:
                await _safe_aclose(agen)
        raise RateLimitError(
            f"all {len(errors)} aggregated members failed: {'; '.join(errors)}",
        )

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[Tool] | None = None,
    ) -> int:
        # Best-effort: delegate to the first resolvable member. Token
        # counts across members differ; documented.
        for member_id in self._members:
            try:
                llm, resolved = await self._resolve(member_id)
            except NotFoundError:
                continue
            if isinstance(llm, AggregatedLLM):
                # INTENTIONAL divergence from stream()'s hard BadRequestError
                # on a nested/self-referential member: count_tokens is a
                # best-effort hot-path estimate, so we skip and try the next
                # member rather than raising strict validation here.
                continue
            return await llm.count_tokens(
                model=resolved.model_name, messages=messages, tools=tools,
            )
        raise ConfigError(
            f"aggregated model profile {self._profile.id!r} has no resolvable "
            f"member for count_tokens",
        )

    async def aclose(self) -> None:
        # No-op: the ProviderRegistry owns downstream adapter lifecycles.
        return


__all__ = ["AggregatedLLM"]
