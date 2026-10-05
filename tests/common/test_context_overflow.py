"""The shared context-overflow classifier: right on the provider texts that really occur, in both directions.

A false POSITIVE is destructive (the executor force-compacts the history away and the replay hits the same
400, every turn); a false NEGATIVE leaves a recoverable overflow as a failed turn. Both directions are pinned
here with the messages the providers actually send, including the ones that mention ``max_tokens`` without
being an overflow.
"""

from __future__ import annotations

import pytest

from primer.common.context_overflow import is_context_overflow, output_cap_never_fits
from primer.model.chat import Error, TurnStreamFailure
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    RateLimitError,
    ServerError,
)

# What the providers send when the PROMPT does not fit.
REAL_OVERFLOWS = [
    pytest.param(
        "This model's maximum context length is 128000 tokens. However, your messages resulted in "
        "130211 tokens. Please reduce the length of the messages.",
        id="openai",
    ),
    pytest.param("prompt is too long: 250017 tokens > 200000 maximum", id="anthropic-prompt"),
    pytest.param(
        "input length and `max_tokens` exceed context limit: 188240 + 21333 > 200000, "
        "decrease input length or `max_tokens` and try again",
        id="anthropic-input-plus-max-tokens",
    ),
    pytest.param("Input is too long for requested model.", id="bedrock"),
    pytest.param(
        "The input token count (1100000) exceeds the maximum number of tokens allowed (1048575).",
        id="gemini",
    ),
    pytest.param("the request exceeds the available context size, try increasing it", id="llama-cpp"),
    pytest.param(
        "This model's maximum prompt length is 131072 but the request contains 150000 tokens.",
        id="xai",
    ),
    pytest.param("Please reduce the length of the messages or completion.", id="groq"),
    pytest.param("too many tokens: total number of tokens in the prompt cannot exceed 4081", id="cohere"),
    pytest.param(
        "Your input exceeds the context window of this model. Please adjust your input and try again.",
        id="openai-responses",
    ),
    pytest.param("context_length_exceeded: the request is too big for this model", id="code-in-the-message"),
    pytest.param(
        "Trying to keep the first 6000 tokens when context the overflows. However, the model is loaded "
        "with context length of only 4096 tokens and cannot continue.",
        id="lm-studio",
    ),
    # vLLM v0.11 serving_engine.py: raised whenever max_tokens is set and input + max_tokens > max_model_len.
    # primer always sends max_output_tokens, so this is THE normal overflow form on a vLLM profile. The cap
    # (4096) fits in the context (32768); it is the history (29000) that does not: compaction fixes it.
    pytest.param(
        "'max_tokens' or 'max_completion_tokens' is too large: 4096. This model's maximum context length is "
        "32768 tokens and your request has 29000 input tokens (4096 > 32768 - 29000).",
        id="vllm-cap-fits-the-history-does-not",
    ),
    # One below the boundary (the cap alone still fits), via the trailer only and via the two separate numbers.
    pytest.param(
        "'max_tokens' is too large: 8191. This model's maximum context length is 8192 tokens and your request "
        "has 50 input tokens (8191 > 8192 - 50).",
        id="vllm-cap-one-below-the-context",
    ),
    pytest.param(
        "'max_tokens' is too large: 4096. This model's maximum context length is 32768 tokens.",
        id="vllm-no-trailer-cap-fits",
    ),
    pytest.param(
        "max_tokens is too large to leave room for the history; exceeds maximum context length (4096 > 32768 - 29000)",
        id="synthetic-trailer-only",
    ),
    # vLLM's renderer (vllm/renderers/params.py, _token_len_check and _text_len_check), the form that replaced
    # the "is too large" one: the cap alone fits (65535 < 128000), the history does not, so compaction can fix it.
    pytest.param(
        "This model's maximum context length is 128000 tokens. However, you requested 65535 output tokens and "
        "your prompt contains at least 62466 input tokens, for a total of at least 128001 tokens. Please reduce "
        "the length of the input prompt or the number of requested output tokens.",
        id="vllm-renderer-tokens",
    ),
    pytest.param(
        "This model's maximum context length is 32768 tokens. However, you requested 4096 output tokens and "
        "your prompt contains 200000 characters (more than 130688 characters, which is the upper bound for "
        "28672 input tokens). Please reduce the length of the input prompt or the number of requested output "
        "tokens.",
        id="vllm-renderer-characters",
    ),
    # vLLM's input processor: the input alone is longer than the context, with no mention of an output cap.
    pytest.param(
        "The decoder prompt (length 5951) is longer than the maximum model length of 4096. Make sure that "
        "`max_model_len` is no smaller than the number of text tokens.",
        id="vllm-decoder-prompt",
    ),
    pytest.param(
        "The prompt (total length 25938) is too long to fit into the model (context length 4096). Make sure that "
        "`max_model_len` is no smaller than the number of text tokens plus multimodal tokens.",
        id="vllm-prompt-too-long-to-fit",
    ),
    # Numbers with thousands separators. Truncating at the comma read the cap as 2 and the context as 1 here, and
    # called a recoverable overflow an output-cap error.
    pytest.param(
        "'max_tokens' is too large: 2,000. This model's maximum context length is 1,000,000 tokens and your "
        "request has 999,500 input tokens (2,000 > 1,000,000 - 999,500).",
        id="vllm-comma-numbers-cap-fits",
    ),
    pytest.param(
        "This model's maximum context length is 1,048,576 tokens. However, you requested 2,000 output tokens and "
        "your prompt contains 1,047,000 input tokens, for a total of 1,049,000 tokens.",
        id="vllm-renderer-comma-numbers-cap-fits",
    ),
    # The OpenAI SDK embeds the response body in the exception text, so an unrelated "too large" can sit next to
    # 'param': 'max_tokens'. Only "<param> is too large" is the output-cap signal (synthetic).
    pytest.param(
        "Error code: 400 - {'error': {'message': \"This model's maximum context length is 8192 tokens. However, "
        "your messages resulted in 9000 tokens. Please reduce the length of the messages. (request body too "
        "large)\", 'type': 'invalid_request_error', 'param': 'max_tokens', 'code': None}}",
        id="sdk-embedded-body-with-an-unrelated-too-large",
    ),
    # Phrases the previous heuristic already matched: kept, so recall does not regress.
    pytest.param("Request is over the maximum context of this endpoint", id="legacy-maximum-context"),
    pytest.param("Total of 9000 tokens exceeds the limit for this model", id="legacy-tokens-exceeds"),
    pytest.param("Request is above the token limit for this model", id="legacy-token-limit"),
]

# What the providers send when the OUTPUT cap or a parameter is wrong. Not an overflow: compacting the
# history cannot fix any of these.
NOT_OVERFLOWS = [
    pytest.param(
        "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens for "
        "claude-sonnet-4-20250514",
        id="anthropic-output-cap",
    ),
    pytest.param(
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead.",
        id="openai-unsupported-max-tokens",
    ),
    pytest.param(
        "max_tokens is too large: 100000. This model supports at most 4096 completion tokens, "
        "whereas you provided 100000.",
        id="openai-max-tokens-too-large",
    ),
    pytest.param(
        "Unable to submit request because it has a maxOutputTokens value of 100000 but the supported "
        "range is from 1 (inclusive) to 8193 (exclusive).",
        id="gemini-output-cap",
    ),
    # A budget phrase ("token limit") is not an overflow when the message is about the OUTPUT side; one
    # sample per output word, so each is pinned on its own.
    pytest.param("Output token limit reached for this request", id="budget-phrase-output-token"),
    pytest.param("Completion token limit is too low for this model", id="budget-phrase-completion-token"),
    pytest.param("max_output_tokens is above the token limit", id="budget-phrase-max-output-tokens"),
    pytest.param("maxOutputTokens is above the token limit", id="budget-phrase-maxoutputtokens"),
    pytest.param("max_completion_tokens is above the token limit", id="budget-phrase-max-completion-tokens"),
    # vLLM, the OTHER case: the cap alone is >= the context length, so no history, however short, can fit.
    pytest.param(
        "'max_tokens' or 'max_completion_tokens' is too large: 100000. This model's maximum context length "
        "is 8192 tokens and your request has 50 input tokens (100000 > 8192 - 50).",
        id="vllm-cap-exceeds-the-context",
    ),
    pytest.param(
        "'max_tokens' is too large: 8192. This model's maximum context length is 8192 tokens and your request "
        "has 50 input tokens (8192 > 8192 - 50).",
        id="vllm-cap-equals-the-context",
    ),
    pytest.param(
        "'max_tokens' is too large: 100000. This model's maximum context length is 8192 tokens.",
        id="vllm-no-trailer-cap-exceeds-the-context",
    ),
    # vLLM's renderer, the OTHER case: the requested output alone is >= the context length, so no history, however
    # short, can fit beside it (an equal cap, a larger cap, and the same with separators).
    pytest.param(
        "This model's maximum context length is 128000 tokens. However, you requested 128000 output tokens and "
        "your prompt contains 10 input tokens, for a total of 128010 tokens. Please reduce the length of the "
        "input prompt or the number of requested output tokens.",
        id="vllm-renderer-cap-equals-the-context",
    ),
    pytest.param(
        "This model's maximum context length is 128000 tokens. However, you requested 200000 output tokens and "
        "your prompt contains 10 input tokens, for a total of 200010 tokens.",
        id="vllm-renderer-cap-exceeds-the-context",
    ),
    pytest.param(
        "This model's maximum context length is 128,000 tokens. However, you requested 130,000 output tokens and "
        "your prompt contains 10 input tokens, for a total of 130,010 tokens.",
        id="vllm-renderer-comma-numbers-cap-exceeds-the-context",
    ),
    pytest.param(
        "'max_tokens' is too large: 130,000. This model's maximum context length is 128,000 tokens and your "
        "request has 50 input tokens (130,000 > 128,000 - 50).",
        id="vllm-comma-numbers-cap-exceeds-the-context",
    ),
    # One per output parameter, so each name in the veto is pinned on its own (no context number: synthetic).
    pytest.param("max_tokens is too large for this model's context length of 8192 tokens", id="veto-max-tokens"),
    pytest.param(
        "max_completion_tokens is too large for this model's context length of 8192 tokens",
        id="veto-max-completion-tokens",
    ),
    pytest.param(
        "max_output_tokens is too large for this model's context length of 8192 tokens", id="veto-max-output-tokens",
    ),
    pytest.param(
        "maxOutputTokens is too large for this model's context length of 8192 tokens", id="veto-maxoutputtokens",
    ),
    pytest.param("Invalid 'metadata.key': string too long. Expected at most 64 characters.", id="bare-too-long"),
    pytest.param("messages: text content blocks must be non-empty", id="unrelated-400"),
]


@pytest.mark.parametrize("message", REAL_OVERFLOWS)
def test_a_raised_bad_request_with_a_real_overflow_text_is_an_overflow(message: str) -> None:
    assert is_context_overflow(BadRequestError(message, status_code=400)) is True


@pytest.mark.parametrize("message", NOT_OVERFLOWS)
def test_a_raised_bad_request_about_the_output_cap_or_a_parameter_is_not_an_overflow(message: str) -> None:
    assert is_context_overflow(BadRequestError(message, status_code=400)) is False


def test_a_provider_overflow_code_wins_over_the_message() -> None:
    assert is_context_overflow(
        BadRequestError("Something went wrong", code="context_length_exceeded", status_code=400),
    ) is True


def test_only_a_bad_request_shaped_failure_can_be_an_overflow() -> None:
    text = "This model's maximum context length is 128000 tokens."
    assert is_context_overflow(RateLimitError(text)) is False
    assert is_context_overflow(AuthenticationError(text)) is False
    assert is_context_overflow(ValueError(text)) is False


def _failure(code: str | None, message: str, *, fatal: bool = True) -> TurnStreamFailure:
    return TurnStreamFailure(
        Error(code=code, message=message, fatal=fatal), partial_messages=[], rounds_completed=0,
    )


@pytest.mark.parametrize("message", REAL_OVERFLOWS)
def test_a_yielded_bad_request_error_with_an_overflow_text_is_an_overflow(message: str) -> None:
    """Ollama and Gemini open the request lazily: the 400 arrives as a yielded Error, not a raise."""
    assert is_context_overflow(_failure("bad_request", message)) is True


@pytest.mark.parametrize("message", NOT_OVERFLOWS)
def test_a_yielded_bad_request_error_about_the_output_cap_is_not_an_overflow(message: str) -> None:
    assert is_context_overflow(_failure("bad_request", message)) is False


def test_a_yielded_provider_overflow_code_is_an_overflow() -> None:
    assert is_context_overflow(_failure("context_length_exceeded", "opaque")) is True


@pytest.mark.parametrize("code", ["rate_limit", "server_error", "network_error", "auth_error", None])
def test_a_yielded_error_of_another_kind_is_not_an_overflow_whatever_it_says(code: str | None) -> None:
    assert is_context_overflow(_failure(code, "maximum context length is 8192 tokens")) is False


def test_a_yielded_error_that_is_not_terminal_is_not_an_overflow() -> None:
    assert is_context_overflow(_failure("bad_request", "context length exceeded", fatal=False)) is False


def test_the_summariser_wrapped_overflow_is_an_overflow() -> None:
    """compaction.py re-raises a yielded fatal Error as ServerError(code=<the Error's code>)."""
    wrapped = ServerError(
        "compaction LLM failed: The input token count (1100000) exceeds the maximum number of tokens "
        "allowed (1048575).",
        code="bad_request",
    )
    assert is_context_overflow(wrapped) is True


def test_a_genuine_summariser_server_error_is_not_an_overflow() -> None:
    assert is_context_overflow(ServerError("compaction LLM failed: boom", code="server_error")) is False
    assert is_context_overflow(ServerError("compaction produced empty summary text")) is False


@pytest.mark.parametrize(
    ("cap", "context", "never_fits"),
    [
        (None, 8192, False),      # no cap configured: the provider's default, nothing to refuse
        (4096, 8192, False),      # the cap fits with room for a history
        (8191, 8192, False),      # one token of room is still room: the message arithmetic, not this, decides
        (8192, 8192, True),       # the cap alone fills the window: no history can fit beside it
        (9000, 8192, True),
        (4096, None, False),      # an unknown window cannot prove anything
        (4096, 0, False),
    ],
)
def test_an_output_cap_that_fills_the_context_window_can_never_fit(
    cap: int | None, context: int | None, never_fits: bool,
) -> None:
    """The executor-side half of the arithmetic veto: when the configured cap is not below the window, no history
    can fit beside it, so compacting the history cannot fix the 400 and the executor must not try."""
    assert output_cap_never_fits(cap, context) is never_fits
