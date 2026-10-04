"""Shared context-overflow classifier.

Answers one question for every place that decides what to do about a prompt
that does not fit the model's context window. Today the one caller is
``_BaseAgentExecutor.invoke``'s hard-overflow recovery (force-compact, then
replay), which recovers only the first shape below; the overflow-replay and
summariser-retry units are meant to call the same function for the others.

A false POSITIVE is destructive: the recovery rewrites the persisted history
into a summary, the replay hits the same 400 (an output-cap or parameter
error does not care how short the history is), and that repeats on every
turn. A false NEGATIVE leaves a recoverable overflow as a failed turn. So
the matching is deliberately conservative: it prefers a provider error code,
matches only phrases that are about the INPUT not fitting, never matches a
bare ``max_tokens`` (the name of the OUTPUT cap), and treats a message that
calls an output-cap parameter "too large" as an output-cap error even when it
also quotes the context length.

An overflow reaches the caller in three shapes, all classified here:

* a raised :class:`~primer.model.except_.BadRequestError` (Anthropic, OpenChat,
  OpenResponses and OpenRouter open the request with an awaited call that
  raises, so the 400 raises). ``invoke`` recovers this shape;
* a YIELDED fatal ``Error(code="bad_request")`` that the loop turns into a
  :class:`~primer.model.chat.TurnStreamFailure` (Ollama and Gemini open the
  request lazily, so the 400 arrives on the first iteration). Classified, NOT
  yet recovered: a replay from scratch would re-run executed tools, and the
  yielded ``Error`` has already been streamed and recorded;
* the same yielded ``Error`` re-raised by the summariser as a
  :class:`~primer.model.except_.ServerError` carrying the ``Error``'s code.

An aggregated profile whose members ALL overflowed re-raises the last
member's ``BadRequestError`` (``primer.llm.aggregated``) so it lands in the
first shape rather than as a ``RateLimitError``.
"""

from __future__ import annotations

from primer.model.chat import Error, TurnStreamFailure
from primer.model.except_ import BadRequestError, ServerError

# Provider-native codes that mean exactly "the prompt does not fit". OpenAI,
# Azure OpenAI and the OpenAI-compatible servers send this one; Anthropic has
# no overflow-specific code and is matched by its message.
OVERFLOW_CODES: frozenset[str] = frozenset({"context_length_exceeded"})

# The code every adapter's classifier gives a 4xx it could not name better,
# and the only generic code a YIELDED overflow is accepted under.
_BAD_REQUEST = "bad_request"

# Phrases that are only ever about the INPUT not fitting. One is enough.
#   openai/azure/vllm : "This model's maximum context length is N tokens..."
#   anthropic         : "prompt is too long: N tokens > M maximum" and
#                       "input length and `max_tokens` exceed context limit: A + B > C"
#   bedrock           : "Input is too long for requested model."
#   gemini            : "The input token count (N) exceeds the maximum number of tokens allowed (M)."
#   llama.cpp / lmstudio : "the request exceeds the available context size"
#   xai               : "This model's maximum prompt length is N but the request contains M tokens."
#   groq              : "Please reduce the length of the messages or completion."
_INPUT_PHRASES: tuple[str, ...] = (
    "context length",
    "context_length",
    "context window",
    "context limit",
    "context size",
    "maximum context",
    "prompt is too long",
    "input is too long",
    "maximum prompt length",
    "input token count",
    "reduce the length of the messages",
)

# Phrases about a token budget that do not say which side overflowed
# (cohere: "too many tokens: total number of tokens in the prompt cannot
# exceed N"). They count only when the message does not talk about OUTPUT
# tokens, so "requested output token limit exceeds ..." stays a non-overflow.
_BUDGET_PHRASES: tuple[str, ...] = ("token limit", "tokens exceeds", "too many tokens")
_OUTPUT_WORDS: tuple[str, ...] = (
    "output token",
    "completion token",
    "max_output_tokens",
    "maxoutputtokens",
    "max_completion_tokens",
)


# The request's OUTPUT-cap parameters. A message that says one of them "is too large" is about the
# request's own output budget, even when it also quotes the context length (vLLM: "'max_tokens' or
# 'max_completion_tokens' is too large: 100000. This model's maximum context length is 8192 tokens and
# your request has 50 input tokens (100000 > 8192 - 50)."). No compaction of the history can fix it.
_OUTPUT_PARAMS: tuple[str, ...] = ("max_tokens", "max_completion_tokens", "max_output_tokens", "maxoutputtokens")


def _message_says_overflow(message: str | None) -> bool:
    text = (message or "").lower()
    if "too large" in text and any(param in text for param in _OUTPUT_PARAMS):
        return False
    if any(phrase in text for phrase in _INPUT_PHRASES):
        return True
    if any(phrase in text for phrase in _BUDGET_PHRASES):
        return not any(word in text for word in _OUTPUT_WORDS)
    return False


def _yielded_says_overflow(code: str | None, message: str | None) -> bool:
    """A yielded (or summariser-wrapped) failure: the code must already say 400-or-overflow.

    An Error with ``rate_limit``, ``server_error``, ``auth_error``, ``network_error`` or no code is
    never an overflow whatever its text says.
    """
    if code in OVERFLOW_CODES:
        return True
    return code == _BAD_REQUEST and _message_says_overflow(message)


def is_context_overflow_error(error: Error) -> bool:
    """True when a terminal ``Error`` event, as an adapter yielded it, is a context overflow."""
    return error.fatal and _yielded_says_overflow(error.code, error.message)


def is_context_overflow(exc: BaseException) -> bool:
    """True when ``exc`` is the provider saying the prompt does not fit the context window."""
    if isinstance(exc, BadRequestError):
        return exc.code in OVERFLOW_CODES or _message_says_overflow(exc.message)
    if isinstance(exc, TurnStreamFailure):
        return is_context_overflow_error(exc.error)
    if isinstance(exc, ServerError):
        return _yielded_says_overflow(exc.code, exc.message)
    return False


__all__ = ["OVERFLOW_CODES", "is_context_overflow", "is_context_overflow_error"]
