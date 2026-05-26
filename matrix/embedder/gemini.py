"""Gemini embedder adapter — wraps google-genai's embed_content.

Subclasses :class:`matrix.int.Embedder`. Honors task_type, title,
auto_truncate, document_ocr, audio_track_extraction from
:class:`ExtendedEmbeddingConfig`. Reuses GoogleConfig from sub-project
#4 (LLM side) and classify_google_exception from
matrix.common.google_errors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from google import genai
from google.genai import types as gtypes

from matrix.common.google_errors import classify_google_exception
from matrix.int.embedder import Embedder
from matrix.model.chat import (
    AudioPart,
    DocumentPart,
    ImagePart,
    VideoPart,
)
from matrix.model.embedding import (
    EmbedResponse,
    Embedding,
    EmbeddingPart,
    EmbeddingUsage,
    ExtendedEmbeddingConfig,
    ExtendedEmbeddingMetadata,
    ExtendedEmbeddingPart,
    PerInputUsage,
    TextPart,
    TokensPart,
)
from matrix.model.except_ import (
    ConfigError,
    ModelNotFoundError,
    UnsupportedContentError,
)
from matrix.model.provider import (
    EmbeddingProvider,
    EmbeddingProviderType,
    GoogleConfig,
)


logger = logging.getLogger(__name__)


_TASK_TYPE_MAP: dict[str, str] = {
    "semantic_similarity": "SEMANTIC_SIMILARITY",
    "classification": "CLASSIFICATION",
    "clustering": "CLUSTERING",
    "retrieval_document": "RETRIEVAL_DOCUMENT",
    "retrieval_query": "RETRIEVAL_QUERY",
    "question_answering": "QUESTION_ANSWERING",
    "fact_verification": "FACT_VERIFICATION",
    "code_retrieval_query": "CODE_RETRIEVAL_QUERY",
}


def _part_to_text(part: EmbeddingPart) -> str:
    """Translate one universal :class:`EmbeddingPart` into a text string.

    Gemini text embedder is text-only — every other modality raises.
    """
    if isinstance(part, TextPart):
        return part.text
    if isinstance(part, ImagePart):
        raise UnsupportedContentError(
            "Gemini text embedder is text-only; image not supported"
        )
    if isinstance(part, ExtendedEmbeddingPart):
        ext = part.extended
        if isinstance(ext, TokensPart):
            raise UnsupportedContentError(
                "Gemini does not support pre-tokenised input"
            )
        type_name = type(ext).__name__
        if "Audio" in type_name:
            raise UnsupportedContentError("audio not supported")
        if "Video" in type_name:
            raise UnsupportedContentError("video not supported")
        if "Document" in type_name:
            raise UnsupportedContentError("document not supported")
        raise UnsupportedContentError(  # pragma: no cover
            f"unsupported extended type {type_name}"
        )
    raise UnsupportedContentError(  # pragma: no cover
        f"unexpected part type {type(part).__name__}"
    )


def _extract_embed_config(
    output_dimensions: int | None,
    config: ExtendedEmbeddingConfig | None,
) -> gtypes.EmbedContentConfig | None:
    """Build EmbedContentConfig from universal knobs. Returns None if
    nothing is set."""
    kwargs: dict[str, Any] = {}
    if output_dimensions is not None:
        kwargs["output_dimensionality"] = output_dimensions
    if config is not None:
        if config.task_type is not None:
            mapped = _TASK_TYPE_MAP.get(config.task_type, config.task_type.upper())
            kwargs["task_type"] = mapped
        if config.title is not None:
            kwargs["title"] = config.title
        if config.auto_truncate is not None:
            kwargs["auto_truncate"] = config.auto_truncate
        if config.document_ocr is not None:
            kwargs["document_ocr"] = config.document_ocr
        if config.audio_track_extraction is not None:
            kwargs["audio_track_extraction"] = config.audio_track_extraction
    if not kwargs:
        return None
    return gtypes.EmbedContentConfig(**kwargs)


def _translate_response(model: str, resp: Any) -> EmbedResponse:
    """Translate a Gemini EmbedContentResponse to a universal EmbedResponse."""
    embeddings: list[Embedding] = []
    for i, item in enumerate(resp.embeddings):
        per_input_usage: PerInputUsage | None = None
        stats = getattr(item, "statistics", None)
        if stats is not None:
            per_input_usage = PerInputUsage(
                token_count=getattr(stats, "token_count", None),
                truncated=getattr(stats, "truncated", None),
            )
        extended = (
            ExtendedEmbeddingMetadata(usage=per_input_usage)
            if per_input_usage is not None
            else None
        )
        values = list(getattr(item, "values", None) or [])
        embeddings.append(Embedding(index=i, vector=values, extended=extended))

    usage: EmbeddingUsage | None = None
    metadata = getattr(resp, "metadata", None)
    if metadata is not None:
        char_count = getattr(metadata, "billable_character_count", None)
        if char_count is not None:
            usage = EmbeddingUsage(
                input_tokens=None,
                input_characters=char_count,
            )

    return EmbedResponse(model=model, embeddings=embeddings, usage=usage)


class GeminiEmbedder(Embedder):
    """Embedding adapter for the Gemini API embed_content endpoint."""

    def __init__(self, provider: EmbeddingProvider) -> None:
        if provider.provider != EmbeddingProviderType.GEMINI:
            raise ConfigError(
                f"GeminiEmbedder requires provider type GEMINI; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, GoogleConfig):
            raise ConfigError(
                "GeminiEmbedder requires GoogleConfig in provider.config"
            )
        # api_key is optional on the config so operators can register
        # endpoints fronted by an auth-injecting proxy; the real
        # Gemini API will surface 401 at call time if the key is
        # actually required.

        self._provider = provider
        self._config: GoogleConfig = provider.config
        self._client: genai.Client | None = None
        self._semaphore = asyncio.Semaphore(provider.limits.max_concurrency)

        logger.info(
            "Gemini embedder initialized",
            extra={
                "provider_id": provider.id,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
            },
        )

    async def list_models(self) -> Iterable[str]:
        return [m.name for m in self._provider.models]

    def _get_client(self) -> genai.Client:
        if self._client is None:
            key = (
                self._config.api_key.get_secret_value()
                if self._config.api_key is not None
                else ""
            )
            self._client = genai.Client(api_key=key)
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

        # Fast-fail on unsupported parts BEFORE acquiring semaphore.
        texts = [_part_to_text(p) for p in inputs]
        embed_config = _extract_embed_config(output_dimensions, config)

        logger.info(
            "Gemini embed starting",
            extra={
                "provider_id": self._provider.id,
                "model": model,
                "input_count": len(inputs),
                "output_dimensions": output_dimensions,
            },
        )

        async with self._semaphore:
            client = self._get_client()
            try:
                resp = await client.aio.models.embed_content(
                    model=model,
                    contents=texts,
                    config=embed_config,
                )
            except Exception as exc:
                err = classify_google_exception(exc)
                logger.error(
                    "Gemini embed failed",
                    extra={
                        "provider_id": self._provider.id,
                        "model": model,
                        "exception": type(exc).__name__,
                    },
                )
                raise err from exc

        return _translate_response(model, resp)
