"""OpenChat LLM adapter — wraps the OpenAI Chat Completions API.

Subclasses :class:`primer.int.LLM` and translates the universal chat
interface (:mod:`primer.model.chat`) onto the legacy OpenAI
``/v1/chat/completions`` wire format. Targets real OpenAI, LM Studio,
Ollama's OpenAI shim, vLLM, and any other compatible server via the
:class:`OpenChatFlavor` discriminator on the provider config.

Parallel structure to :mod:`primer.llm.openresponses`. Shared helpers
live in :mod:`primer.llm._openai_common`.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from primer.int.llm import LLM
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    Message,
    Part,
    TextPart,
    Tool,
    ToolCallPart,
    ToolChoice,
    ToolResultPart,
    VideoPart,
)
from primer.model.except_ import ConfigError, UnsupportedContentError
from primer.model.provider import (
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FlavorPolicy:
    """Per-flavor behavioural knobs for the OpenChat adapter.

    Attributes
    ----------
    require_api_key
        When True, an absent or empty ``api_key`` raises
        :class:`ConfigError` at construction time.
    """

    require_api_key: bool


_POLICY_BY_FLAVOR: dict[OpenChatFlavor, _FlavorPolicy] = {
    OpenChatFlavor.OPENAI: _FlavorPolicy(require_api_key=True),
    OpenChatFlavor.LMSTUDIO: _FlavorPolicy(require_api_key=False),
    OpenChatFlavor.OLLAMA: _FlavorPolicy(require_api_key=False),
    OpenChatFlavor.VLLM: _FlavorPolicy(require_api_key=False),
    OpenChatFlavor.OTHER: _FlavorPolicy(require_api_key=True),
}


def _part_to_content(part: Part) -> dict[str, Any]:
    """Translate one universal :class:`Part` into a Chat Completions content dict.

    Pure function, no I/O. Raises :class:`UnsupportedContentError` for
    parts the Chat Completions API does not accept.
    """
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}

    if isinstance(part, ImagePart):
        if part.file_id is not None:
            raise UnsupportedContentError(
                "Chat Completions does not accept image input by file_id; "
                "fetch the bytes and pass an ImagePart(data=...) instead"
            )
        if part.data is not None:
            mime = part.mime_type or "application/octet-stream"
            url = f"data:{mime};base64,{base64.b64encode(part.data).decode()}"
        else:
            url = part.url  # type: ignore[assignment]
        image_url: dict[str, Any] = {"url": url}
        if part.detail is not None:
            image_url["detail"] = part.detail
        return {"type": "image_url", "image_url": image_url}

    if isinstance(part, DocumentPart):
        raise UnsupportedContentError(
            "Chat Completions does not accept document input; "
            "extract text from the document and pass a TextPart instead"
        )

    if isinstance(part, ExtendedPart):
        ext = part.extended
        if isinstance(ext, AudioPart):
            raise UnsupportedContentError(
                "Chat Completions does not accept audio input on this adapter"
            )
        if isinstance(ext, VideoPart):
            raise UnsupportedContentError(
                "Chat Completions does not accept video input"
            )
        raise UnsupportedContentError(
            f"Chat Completions does not support extended part type {ext.type!r}"
        )

    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _messages_to_chat(messages: list[Message]) -> list[dict[str, Any]]:
    """Walk a chat history and produce Chat Completions ``messages`` rows.

    Mapping rules:

    * ``role="system"`` -> one row with string ``content`` joining all
      :class:`TextPart` text values.
    * ``role="user"`` -> if the message is text-only, ``content`` is a
      plain string; if any image part is present, ``content`` is the
      multimodal content array.
    * ``role="assistant"`` -> text concatenated into ``content`` (or
      ``None`` when there is no text), with any :class:`ToolCallPart`
      flattened into the ``tool_calls`` array.
    * ``role="tool"`` -> one row per :class:`ToolResultPart`, each with
      ``tool_call_id`` echoing the assistant's call id.
    """
    rows: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "tool":
            for part in msg.parts:
                if not isinstance(part, ToolResultPart):
                    raise UnsupportedContentError(
                        f"tool-role messages must contain only ToolResultPart; "
                        f"got {type(part).__name__}"
                    )
                rows.append(
                    {
                        "role": "tool",
                        "tool_call_id": part.id,
                        "content": part.output,
                    }
                )
            continue

        text_chunks: list[str] = []
        non_text_contents: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []

        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                tool_calls.append(
                    {
                        "id": part.id,
                        "type": "function",
                        "function": {
                            "name": part.name,
                            "arguments": json.dumps(part.arguments),
                        },
                    }
                )
            elif isinstance(part, ToolResultPart):
                raise UnsupportedContentError(
                    "ToolResultPart is only valid inside a tool-role message"
                )
            elif isinstance(part, TextPart):
                text_chunks.append(part.text)
            else:
                non_text_contents.append(_part_to_content(part))

        if non_text_contents:
            content: Any = [
                {"type": "text", "text": "".join(text_chunks)}
            ] if text_chunks else []
            content.extend(non_text_contents)
        elif text_chunks:
            content = "".join(text_chunks)
        else:
            content = None

        row: dict[str, Any] = {"role": msg.role, "content": content}
        if tool_calls:
            row["tool_calls"] = tool_calls
        rows.append(row)

    return rows


def _tool_to_chat(tool: Tool) -> dict[str, Any]:
    """Translate a universal :class:`Tool` into one Chat Completions tool dict.

    The Chat Completions envelope nests the function-spec fields under
    ``function:`` — unlike the Responses envelope which inlines them.
    ``tool.toolset_id`` is caller-side correlation only and is not
    transmitted.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.id,
            "description": tool.description,
            "parameters": tool.args_schema,
        },
    }


def _tool_choice_to_chat(choice: ToolChoice | None) -> Any:
    """Translate the universal :data:`ToolChoice` to the Chat Completions value.

    Returns ``None`` to signal "do not include in the request"; the
    caller must drop the key from the payload.
    """
    if choice is None:
        return None
    if choice in ("auto", "required", "none"):
        return choice
    return {"type": "function", "function": {"name": choice}}


class OpenChatLLM(LLM):
    """Streaming LLM adapter for the OpenAI Chat Completions API."""

    def __init__(self, provider: LLMProvider) -> None:
        if provider.provider != LLMProviderType.OPENCHAT:
            raise ConfigError(
                f"OpenChatLLM requires provider type OPENCHAT; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, OpenChatConfig):
            raise ConfigError(
                "OpenChatLLM requires OpenChatConfig in provider.config"
            )

        self._provider = provider
        self._config: OpenChatConfig = provider.config
        self._policy = _POLICY_BY_FLAVOR[provider.config.flavor]

        key_present = (
            provider.config.api_key is not None
            and bool(provider.config.api_key.get_secret_value())
        )
        if self._policy.require_api_key and not key_present:
            raise ConfigError(
                f"api_key is required for flavor={provider.config.flavor.value}"
            )

        self._client: AsyncOpenAI | None = None
        self._max_concurrency = provider.limits.max_concurrency

        logger.info(
            "OpenChat adapter initialized",
            extra={
                "provider_id": provider.id,
                "flavor": provider.config.flavor.value,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
            },
        )

    async def list_models(self) -> Iterable[str]:
        return [m.name for m in self._provider.models]

    def _get_client(self) -> AsyncOpenAI:
        """Construct the AsyncOpenAI client lazily on first use."""
        if self._client is None:
            key = (
                self._config.api_key.get_secret_value()
                if self._config.api_key is not None
                else ""
            ) or "no-key-required"
            self._client = AsyncOpenAI(
                base_url=str(self._config.url),
                api_key=key,
            )
        return self._client

    async def stream(self, **kwargs: Any):  # type: ignore[override]
        """Streaming entrypoint. Filled in across Phases 4-9.

        Yields exactly one :class:`Done` sentinel so the adapter is
        instantiable and an end-to-end smoke can confirm the dispatch
        path is wired. Real translation lands in Phase 8/9.
        """
        from primer.model.chat import Done
        yield Done(stop_reason="stop", raw_reason="stub")
