"""OpenAI embedder adapter — wraps the OpenAI Embeddings API.

Subclasses :class:`matrix.int.Embedder` and translates the universal
embedding interface (:mod:`matrix.model.embedding`) onto the OpenAI
embeddings wire format. Supports both real OpenAI and LM Studio's
OpenAI-compatible endpoint via the :class:`OpenAIEmbeddingFlavor`
discriminator on the provider config.

See the design spec at
``docs/superpowers/specs/2026-04-26-openai-embedder-design.md`` for
the per-Part input mapping, response translation, and flavor policy
details.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from primer.common.openai_errors import classify_openai_exception
from primer.int.coordinator import RateLimiter
from primer.int.embedder import Embedder
from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ImagePart,
    VideoPart,
)
from primer.model.embedding import (
    EmbedResponse,
    Embedding,
    EmbeddingPart,
    EmbeddingUsage,
    ExtendedEmbeddingConfig,
    ExtendedEmbeddingPart,
    TextPart,
    TokensPart,
)
from primer.model.except_ import (
    ConfigError,
    ModelNotFoundError,
    UnsupportedContentError,
)
from primer.model.provider import (
    EmbeddingProvider,
    EmbeddingProviderType,
    OpenAIConfig,
    OpenAIEmbeddingFlavor,
)


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Flavor policy                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FlavorPolicy:
    """Per-flavor behavioural knobs for the OpenAI embedder.

    Resolved once at construction time from
    :class:`OpenAIConfig.flavor` and consulted at the divergence sites.

    Attributes
    ----------
    require_api_key
        When True, an empty ``api_key`` raises :class:`ConfigError` in
        ``__init__``. LM Studio sets this to False because LM Studio
        accepts unauthenticated requests by default — but if the user
        provides a key (e.g. for a reverse proxy), it is still passed
        through to AsyncOpenAI.
    """

    require_api_key: bool


_POLICY_BY_FLAVOR: dict[OpenAIEmbeddingFlavor, _FlavorPolicy] = {
    OpenAIEmbeddingFlavor.OPENAI: _FlavorPolicy(require_api_key=True),
    OpenAIEmbeddingFlavor.LMSTUDIO: _FlavorPolicy(require_api_key=False),
    OpenAIEmbeddingFlavor.OTHER: _FlavorPolicy(require_api_key=True),
}


# --------------------------------------------------------------------------- #
# Input mapping: matrix.model.embedding.EmbeddingPart -> OpenAI input element  #
# --------------------------------------------------------------------------- #


def _part_to_openai_input(part: EmbeddingPart) -> str | list[int]:
    """Translate one universal :class:`EmbeddingPart` into one OpenAI
    embeddings ``input`` element.

    Pure function, no I/O. OpenAI embeddings are text-only and
    pre-tokenised-only; every other modality raises
    :class:`UnsupportedContentError`.
    """
    if isinstance(part, TextPart):
        return part.text

    if isinstance(part, ImagePart):
        raise UnsupportedContentError(
            "OpenAI embeddings are text-only; got image"
        )

    if isinstance(part, ExtendedEmbeddingPart):
        ext = part.extended
        if isinstance(ext, TokensPart):
            return list(ext.tokens)
        if isinstance(ext, AudioPart):
            raise UnsupportedContentError(
                "OpenAI embeddings do not accept audio"
            )
        if isinstance(ext, VideoPart):
            raise UnsupportedContentError(
                "OpenAI embeddings do not accept video"
            )
        if isinstance(ext, DocumentPart):
            raise UnsupportedContentError(
                "OpenAI embeddings do not accept documents; "
                "pre-extract text"
            )
        raise UnsupportedContentError(  # pragma: no cover
            f"OpenAI embeddings do not support extended part type {ext.type!r}"
        )

    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _inputs_to_openai_list(
    inputs: list[EmbeddingPart],
) -> list[str | list[int]]:
    """Walk the universal ``inputs`` list and produce a heterogeneous
    list suitable for OpenAI's ``client.embeddings.create(input=...)``.

    Order is preserved; index correspondence is the caller's contract.
    The first part that is not embeddable raises immediately —
    short-circuit, no partial batch sent.
    """
    return [_part_to_openai_input(part) for part in inputs]


# --------------------------------------------------------------------------- #
# Response translation: OpenAI response -> matrix.model.embedding types        #
# --------------------------------------------------------------------------- #


def _translate_response(resp: Any) -> EmbedResponse:
    """Translate an OpenAI embeddings response into the universal
    :class:`EmbedResponse`.

    Pure function. The SDK returns embeddings in input order with
    explicit ``index`` fields; we preserve those. OpenAI does not
    surface per-input metadata, so :attr:`Embedding.extended` is always
    ``None`` for this adapter. Request-level usage is taken from
    ``resp.usage.prompt_tokens`` (defensive ``getattr`` so a future SDK
    rename leaves ``input_tokens=None`` rather than crashing).
    """
    embeddings = [
        Embedding(
            index=item.index,
            vector=list(item.embedding),
            extended=None,
        )
        for item in resp.data
    ]
    usage: EmbeddingUsage | None = None
    if getattr(resp, "usage", None) is not None:
        usage = EmbeddingUsage(
            input_tokens=getattr(resp.usage, "prompt_tokens", None),
            input_characters=None,
        )
    return EmbedResponse(
        model=resp.model,
        embeddings=embeddings,
        usage=usage,
    )


# --------------------------------------------------------------------------- #
# Adapter                                                                      #
# --------------------------------------------------------------------------- #


class OpenAIEmbedder(Embedder):
    """Embedding adapter for the OpenAI Embeddings API."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if provider.provider != EmbeddingProviderType.OPENAI:
            raise ConfigError(
                f"OpenAIEmbedder requires provider type OPENAI; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, OpenAIConfig):
            raise ConfigError(
                "OpenAIEmbedder requires OpenAIConfig in provider.config"
            )

        self._provider = provider
        self._config: OpenAIConfig = provider.config
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
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"embedder:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency

        logger.info(
            "OpenAI embedder initialized",
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
        """Construct the AsyncOpenAI client lazily on first use.

        Whatever ``api_key`` the user configured (including empty
        string for unauthenticated LM Studio, or a reverse-proxy key)
        is passed straight through.
        """
        if self._client is None:
            # AsyncOpenAI rejects api_key=None outright; pass a sentinel
            # placeholder so unauthenticated endpoints (LM Studio, vLLM)
            # work without forcing a junk key on the operator.
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

    async def embed(  # type: ignore[override]
        self,
        *,
        model: str,
        inputs: list[EmbeddingPart],
        output_dimensions: int | None = None,
        config: ExtendedEmbeddingConfig | None = None,
    ) -> EmbedResponse:
        allowed = {m.name for m in self._provider.models}
        if model not in allowed:
            raise ModelNotFoundError(
                f"model {model!r} is not configured for provider "
                f"{self._provider.id!r}; configured models: {sorted(allowed)}"
            )

        request: dict[str, Any] = {
            "model": model,
            "input": _inputs_to_openai_list(inputs),
        }
        if output_dimensions is not None:
            request["dimensions"] = output_dimensions
        if config is not None and config.user is not None:
            request["user"] = config.user

        logger.info(
            "OpenAI embed starting",
            extra={
                "provider_id": self._provider.id,
                "model": model,
                "input_count": len(inputs),
                "output_dimensions": output_dimensions,
            },
        )

        async with await self._rate_limiter.acquire(
            self._rate_limit_key, max_concurrency=self._max_concurrency,
        ):
            client = self._get_client()
            try:
                resp = await client.embeddings.create(**request)
            except Exception as exc:
                err = classify_openai_exception(exc)
                logger.error(
                    "OpenAI embed failed",
                    extra={
                        "provider_id": self._provider.id,
                        "model": model,
                        "exception": type(exc).__name__,
                    },
                )
                raise err from exc

        return _translate_response(resp)
