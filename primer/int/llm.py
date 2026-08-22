"""Abstract base class for streaming chat LLM providers.

Implementations bind to a configured provider (URL, credentials, rate
limits, etc.) at construction time and may serve multiple models — model
selection happens per call.

The signature was derived from the cross-SDK comparison documented in
``research/abc_interface.md``. Universal parameters (temperature, top_p,
max_output_tokens, stop, response_format, tools, tool_choice) appear on
the abstract method; provider-specific knobs (seed, top_k,
frequency_penalty, reasoning controls, parallel_tool_calls,
prompt_cache_key, safety_settings, ...) live in an open-ended
``extended: dict[str, Any]`` slot the adapter is free to interpret.

See :data:`primer.model.chat.StreamEvent` for the event union the stream
yields. Adapters MUST wrap exceptions into a terminal
:class:`primer.model.chat.Error` event with ``fatal=True`` rather than
propagating, so consumers can rely on the iterator always closing
cleanly.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from typing import Any

from pydantic import BaseModel

from primer.model.chat import Message, StreamEvent, Tool, ToolChoice




def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the one JSON object out of a model's reply.

    Structured output keeps most providers to bare JSON, but reasoning
    models wrap the object in prose or code fences even then. Extract
    rather than reject; raise when no object is present at all, so the
    caller can fail closed instead of acting on a guess.
    """
    candidate = text.strip()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(
                f"no JSON object in judge output: {candidate[:200]!r}"
            ) from None
        parsed = json.loads(candidate[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(
            f"judge output is JSON but not an object: {candidate[:200]!r}"
        )
    return parsed


class LLM(ABC):
    """Provider-agnostic streaming chat LLM interface.

    Subclasses are bound to one configured provider but may dispatch to
    multiple models on it. The ``model`` parameter on :meth:`stream`
    selects which one to use for a given call.

    There is deliberately no ``list_models`` here. It used to enumerate
    ``LLMProvider.models``, the allowlist that
    :class:`~primer.model.model_profile.ModelProfile` replaced; adapters
    also no longer validate ``model`` against that list, because the name
    now reaches them from a persisted profile rather than caller input.
    ``Embedder`` and ``CrossEncoder`` keep their own ``list_models``:
    those families have no profiles and still carry a model list on the
    provider row.
    """


    async def judge_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        user_message: str,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """One non-interactive structured verdict from this provider.

        Used by the LLM approval gate. Implemented here, on the ABC, in
        terms of the one thing every adapter already provides: a
        ``stream()`` that accepts a JSON-schema ``response_format``.
        Providers with native structured output enforce the schema
        server-side; the rest are held to it by the prompt and the
        strict parse below. Temperature is pinned to 0 because a
        verdict must not be sampled hot.

        Raises on a fatal stream error or unparseable output; the
        approval gate treats any raise as fail-closed.
        """
        from primer.model.chat import Error, Message, TextDelta, TextPart

        messages = [
            Message(role="system", parts=[TextPart(text=system_prompt)]),
            Message(role="user", parts=[TextPart(text=user_message)]),
        ]
        chunks: list[str] = []
        async for event in self.stream(
            model=model,
            messages=messages,
            temperature=0.0,
            response_format=response_schema,
        ):
            if isinstance(event, TextDelta):
                chunks.append(event.text)
            elif isinstance(event, Error) and event.fatal:
                raise RuntimeError(
                    f"judge stream failed: {event.message}"
                )
        return _extract_json_object("".join(chunks))


    @abstractmethod
    def stream(
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
        """Stream the model's response as a sequence of :class:`StreamEvent`s.

        Concrete implementations are async generators
        (``async def stream(...): ... yield event``).

        Parameters
        ----------
        model
            Provider-side model identifier. Must be one of the names
            returned by :meth:`list_models`; adapters should validate
            this before dispatch.
        messages
            Ordered chat history. ``system``-role messages are lifted to
            the right provider-specific surface by the adapter
            (Anthropic top-level ``system``, Google
            ``system_instruction``, OpenAI/Ollama inline).
        temperature, top_p
            Universal sampling knobs supported by every adapter.
        max_output_tokens
            Maximum tokens to generate. Adapter renames internally for
            providers that use a different parameter name (Anthropic
            ``max_tokens``, Ollama ``num_predict``). The Anthropic
            adapter supplies a sensible default when ``None`` because
            Anthropic's API requires the parameter.
        stop
            Stop sequences. Honoured by Anthropic / Google / Ollama;
            silently ignored by OpenAI Responses (no native parameter).
        response_format
            Either a Pydantic class (the adapter calls
            ``.model_json_schema()`` to derive the schema) or a raw JSON
            Schema dict. Routes to OpenAI ``text.format.json_schema``,
            Google ``response_schema`` + ``response_mime_type``, Ollama
            ``format``, and is emulated on Anthropic via a forced single
            tool whose ``input_schema`` is the desired output shape.
        tools
            Tool catalogue the model may invoke. Adapter wraps each
            :class:`Tool` into the provider-specific envelope.
        tool_choice
            How the model should decide whether to invoke tools.
            ``"auto"`` / ``"required"`` / ``"none"`` are mode strings;
            any other string names a specific tool to force. Ignored by
            the Ollama adapter (Ollama doesn't expose tool_choice).
        extended
            Provider-specific knobs that don't map cleanly across
            providers (``seed``, ``top_k``, ``frequency_penalty``,
            ``presence_penalty``, ``reasoning_effort``,
            ``parallel_tool_calls``, ``prompt_cache_key``,
            ``safety_settings``, ``thinking_budget``, ...). Adapters
            silently ignore unknown keys.

        Returns
        -------
        AsyncIterator[StreamEvent]
            Async iterator the caller consumes with ``async for``.
            See :data:`primer.model.chat.StreamEvent` for the event
            union. The iterator always yields exactly one terminal
            event (:class:`Done` for success, :class:`Error` with
            ``fatal=True`` for failure) before closing.
        """

    @abstractmethod
    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[Tool] | None = None,
    ) -> int:
        """Estimate prompt token count for ``messages`` (+ optional tools).

        Adapters MUST return a best-effort estimate. Preferred path is
        the provider's native tokenizer or count API. A char-heuristic
        fallback is acceptable when neither is available.

        Used by :func:`primer.agent.compaction_mixin.should_compact`
        to decide whether the next turn needs compaction before
        dispatch. Called on the hot path, so adapters with
        network-based counters (Anthropic, Gemini) MUST cache
        aggressively.
        """

    async def aclose(self) -> None:
        """Release backend resources held by this adapter.

        Default is a no-op. Adapters that hold connection pools or
        long-lived sessions (HTTP client, websocket, subprocess) MUST
        override and close them. Idempotent: calling twice is safe.

        Called by :class:`primer.api.registries.ProviderRegistry` when
        the underlying ``LLMProvider`` row is invalidated and the
        cached adapter is dropped.
        """
        return
