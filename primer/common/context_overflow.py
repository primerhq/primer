"""Shared context-overflow classifier.

Answers one question for every place that decides what to do about a prompt
that does not fit the model's context window. Today the callers are
``_BaseAgentExecutor``'s hard-overflow recovery (force-compact, then continue
the turn), which recovers the first two shapes below, and the agent loop, which
uses :func:`is_context_overflow_error` to hold a yielded overflow back from the
caller that recovers it; the summariser-retry unit is meant to call the same
function for the third.

A false POSITIVE is destructive: the recovery rewrites the persisted history
into a summary, the replay hits the same 400 (an output-cap or parameter
error does not care how short the history is), and that repeats on every
turn. A false NEGATIVE leaves a recoverable overflow as a failed turn. So
the matching is deliberately conservative: it prefers a provider error code,
matches only phrases that are about the INPUT not fitting, never matches a
bare ``max_tokens`` (the name of the OUTPUT cap), and treats a message that
calls an output-cap parameter "too large" as an output-cap error unless its
own numbers show the cap fits the context window (vLLM's normal overflow
form), in which case the history is what does not fit.

An overflow reaches the caller in three shapes, all classified here:

* a raised :class:`~primer.model.except_.BadRequestError` (Anthropic, OpenChat,
  OpenResponses and OpenRouter open the request with an awaited call that
  raises, so the 400 raises). ``invoke`` recovers this shape;
* a YIELDED fatal ``Error(code="bad_request")`` that the loop turns into a
  :class:`~primer.model.chat.TurnStreamFailure` (Ollama and Gemini open the
  request lazily, so the 400 arrives on the first iteration). Recovered too,
  by a caller that opts in: when the stream held nothing but that ``Error`` the
  loop does not yield it (a yielded ``Error`` is a terminal record, and a turn
  that recovers must end with one) and raises
  :class:`~primer.model.chat.TurnStreamOverflow`, which ``invoke`` recovers
  like the raised shape. Content streamed before the error is already out, so
  that case is not recovered;
* the same yielded ``Error`` re-raised by the summariser as a
  :class:`~primer.model.except_.ServerError` carrying the ``Error``'s code.

An aggregated profile whose members ALL overflowed re-raises the last
member's ``BadRequestError`` (``primer.llm.aggregated``) so it lands in the
first shape rather than as a ``RateLimitError``.
"""

from __future__ import annotations

import re

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
#   vllm (input proc.): "The decoder prompt (length 5951) is longer than the maximum model length of 4096."
#                       (the same f-string says "encoder prompt" with the multimodal encoder cache size as the
#                       limit: see _ENCODER_PROMPT, that one is not about the history)
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
    "maximum model length",
    "input token count",
    "reduce the length of the messages",
)

# Phrases about a token budget that do not say which side overflowed
# (cohere: "too many tokens: total number of tokens in the prompt cannot
# exceed N"). They count only when the message does not talk about OUTPUT
# tokens, so "requested output token limit exceeds ..." stays a non-overflow.
_BUDGET_PHRASES: tuple[str, ...] = ("token limit", "tokens exceeds", "too many tokens")

# vLLM's input processor (_validate_prompt_len) words the decoder and the ENCODER check with one f-string, "The
# {prompt_type} prompt (length N) is longer than the maximum model length of M". For the encoder, M is the
# multimodal encoder cache size, not the context window, and the thing that is too long is an image or an audio
# clip, which compacting the text history cannot shrink: the replay would be rejected the same way.
_ENCODER_PROMPT = "encoder prompt"
_OUTPUT_WORDS: tuple[str, ...] = (
    "output token",
    "completion token",
    "max_output_tokens",
    "maxoutputtokens",
    "max_completion_tokens",
)


# A message that says an OUTPUT-cap parameter "is too large" is about the request's own output budget.
# Anchored to "<param> is too large" on purpose: the OpenAI SDK embeds the response body in the
# exception text, so 'param': 'max_tokens' can sit next to an unrelated "too large".
_OUTPUT_PARAM_TOO_LARGE = re.compile(
    r"(?:max_tokens|max_completion_tokens|max_output_tokens|maxoutputtokens)['\"]?\s+is too large"
)

# vLLM (serving_engine.py) raises this whenever max_tokens is set and input + max_tokens > max_model_len:
#   "'max_tokens' or 'max_completion_tokens' is too large: 4096. This model's maximum context length is
#    32768 tokens and your request has 29000 input tokens (4096 > 32768 - 29000)."
# Whether it is an output-cap error or a history that does not fit is ARITHMETIC: if the cap alone is
# already >= the context length no compaction can help; if the cap fits and the request does not, the
# history is what must shrink (and primer always sends a max_output_tokens, so this is the normal
# overflow form on a vLLM-backed profile). OpenAI's "max_tokens is too large: N. This model supports at
# most M completion tokens" carries no context length at all: that one is an output-cap error.
#
# Newer vLLM (vllm/renderers/params.py, _token_len_check / _text_len_check) words the same condition without
# "is too large", and states both numbers too:
#   "This model's maximum context length is 128000 tokens. However, you requested 65535 output tokens and your
#    prompt contains at least 62466 input tokens, for a total of at least 128001 tokens. Please reduce ..."
# The same arithmetic applies: a requested output that is already >= the context length cannot be helped by a
# shorter history. (Numbers are read with their thousands separators: "1,000,000" is a million, not a 1.)
_NUM = r"\d{1,3}(?:,\d{3})+|\d+"  # a comma belongs to the number only before exactly three digits
_VLLM_TRAILER = re.compile(rf"\(({_NUM})\s*>\s*({_NUM})\s*-\s*({_NUM})\)")
_TOO_LARGE_CAP = re.compile(rf"is too large:\s*({_NUM})")
_MAX_CONTEXT = re.compile(rf"maximum context length is\s*({_NUM})")
_REQUESTED_OUTPUT = re.compile(rf"you requested\s*({_NUM})\s*output tokens")


def _int(digits: str) -> int:
    return int(digits.replace(",", ""))


def _cap_and_context(text: str) -> tuple[int, int] | None:
    """The (output cap, context length) a message states, or None when it states no context length."""
    trailer = _VLLM_TRAILER.search(text)
    if trailer:
        return _int(trailer.group(1)), _int(trailer.group(2))
    cap, context = _TOO_LARGE_CAP.search(text), _MAX_CONTEXT.search(text)
    if cap and context:
        return _int(cap.group(1)), _int(context.group(1))
    return None


def _message_says_overflow(message: str | None) -> bool:
    text = (message or "").lower()
    if _ENCODER_PROMPT in text:
        return False
    if _OUTPUT_PARAM_TOO_LARGE.search(text):
        numbers = _cap_and_context(text)
        if numbers is None or numbers[0] >= numbers[1]:
            return False  # no context number, or the cap alone does not fit: an output-cap error
        # The cap fits and the request still does not: fall through, the history is what is too big.
    requested, context = _REQUESTED_OUTPUT.search(text), _MAX_CONTEXT.search(text)
    if requested and context and _int(requested.group(1)) >= _int(context.group(1)):
        return False  # the requested output alone fills the window: no history can fit beside it
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


def output_cap_never_fits(max_output_tokens: int | None, context_length: int | None) -> bool:
    """True when the configured output cap alone is not below the context window.

    Then no history, however short, fits beside the cap: a 400 that says the prompt does not fit is the
    request's own cap talking, and compacting the history away cannot fix it (it would rewrite the persisted
    history into a summary and the replay would be rejected the same way). This is the executor-side half of the
    arithmetic the message-level veto does for the providers that state both numbers; it works for every provider.
    An unset cap or an unknown window proves nothing.
    """
    return bool(max_output_tokens and context_length and max_output_tokens >= context_length)


def output_cap_warning(max_output_tokens: int | None, context_length: int | None) -> str | None:
    """The operator-facing warning for a cap that fills the window, or ``None`` when there is nothing to say.

    Not a refusal: some servers clamp an oversized cap instead of rejecting it, so a configuration that looks
    unusable here can work against them, and the reactive guard (:func:`output_cap_never_fits`) still decides what a
    rejection means. The status endpoint and the start of a turn both say this, once, so the first rejected call is
    not the operator's first sign of it.
    """
    if not output_cap_never_fits(max_output_tokens, context_length):
        return None
    return (
        f"max_output_tokens ({max_output_tokens}) is not below the model's context window ({context_length}): "
        f"no prompt, however short, fits beside that cap, so a provider that checks it rejects every request; "
        f"lower max_output_tokens or use a model with a larger window"
    )


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


__all__ = ["OVERFLOW_CODES", "is_context_overflow", "is_context_overflow_error", "output_cap_never_fits"]
