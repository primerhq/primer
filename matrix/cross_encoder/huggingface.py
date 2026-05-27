"""HuggingFace cross-encoder adapter — wraps sentence-transformers locally.

Subclasses :class:`matrix.int.CrossEncoder`. Mirrors the shape of
:class:`matrix.embedder.huggingface.HuggingFaceEmbedder`: bound to a
configured :class:`CrossEncoderProvider` at construction time, lazy-
loads each requested model on first call, runs the synchronous
:class:`sentence_transformers.CrossEncoder.predict` inside
:func:`asyncio.to_thread` so the event loop stays unblocked.

Score scale is the model's raw logit (no sigmoid). Higher = more
relevant. Callers must treat scores as backend-relative.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from sentence_transformers import CrossEncoder as STCrossEncoder

from matrix.int.coordinator import RateLimiter
from matrix.int.cross_encoder import CrossEncoder
from matrix.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    MatrixError,
    NetworkError,
    ProviderError,
)
from matrix.model.provider import (
    CrossEncoderProvider,
    CrossEncoderProviderType,
    HuggingFaceCrossEncoderConfig,
)


logger = logging.getLogger(__name__)


def _classify_hf_exception(exc: Exception) -> MatrixError:
    """Inline classifier mirroring the embedder's pattern.

    sentence-transformers / huggingface_hub don't share a common
    exception base, so we string-match on the most common error
    surfaces and fall back to ``ProviderError``.
    """
    msg = str(exc)
    type_name = type(exc).__name__
    if (
        "401" in msg
        or "GatedRepoError" in type_name
        or "authentication" in msg.lower()
    ):
        return AuthenticationError(
            "HuggingFace Hub authentication failed",
            cause=exc,
        )
    if "404" in msg or "RepositoryNotFoundError" in type_name:
        return BadRequestError(
            f"HuggingFace model not found: {msg}",
            cause=exc,
        )
    if isinstance(exc, OSError):
        return NetworkError(
            f"HuggingFace I/O failure: {type_name}",
            cause=exc,
        )
    return ProviderError(str(exc), cause=exc)


def _predict_sync(
    model: STCrossEncoder,
    pairs: list[tuple[str, str]],
    batch_size: int,
) -> list[float]:
    """Sync wrapper around CrossEncoder.predict — runs in asyncio.to_thread.

    Returns scores as a plain Python list. ``predict`` returns either a
    numpy array or a list depending on configuration; ``.tolist()``
    handles both cases via numpy's protocol, falling back to ``list()``
    for plain lists.
    """
    raw = model.predict(
        pairs,
        batch_size=batch_size,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return raw.tolist() if hasattr(raw, "tolist") else list(raw)


class HuggingFaceCrossEncoder(CrossEncoder):
    """Local cross-encoder adapter via sentence-transformers."""

    def __init__(
        self,
        provider: CrossEncoderProvider,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if provider.provider != CrossEncoderProviderType.HUGGINGFACE:
            raise ConfigError(
                "HuggingFaceCrossEncoder requires provider type "
                f"HUGGINGFACE; got {provider.provider}"
            )
        if not isinstance(provider.config, HuggingFaceCrossEncoderConfig):
            raise ConfigError(
                "HuggingFaceCrossEncoder requires "
                "HuggingFaceCrossEncoderConfig in provider.config"
            )
        self._provider = provider
        self._config: HuggingFaceCrossEncoderConfig = provider.config
        self._models: dict[str, STCrossEncoder] = {}
        if rate_limiter is None:
            from matrix.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"cross_encoder:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency
        # Resolve max_pair_length per-model from the provider's catalogue
        # so we can pass it to predict if the user set it.
        self._max_pair_length: dict[str, int | None] = {
            m.name: m.max_pair_length for m in provider.models
        }

        logger.info(
            "HuggingFace cross-encoder initialized",
            extra={
                "provider_id": provider.id,
                "models": [m.name for m in provider.models],
                "max_concurrency": provider.limits.max_concurrency,
            },
        )

    async def list_models(self) -> Iterable[str]:
        return [m.name for m in self._provider.models]

    async def _get_model(self, name: str) -> STCrossEncoder:
        if name not in self._max_pair_length:
            raise ConfigError(
                f"model {name!r} is not registered on cross-encoder "
                f"provider {self._provider.id!r}"
            )
        if name not in self._models:
            token_value = (
                self._config.token.get_secret_value()
                if self._config.token is not None
                else None
            )
            kwargs: dict = {}
            max_len = self._max_pair_length.get(name)
            if max_len is not None:
                kwargs["max_length"] = max_len
            try:
                self._models[name] = await asyncio.to_thread(
                    STCrossEncoder,
                    name,
                    token=token_value,
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                raise _classify_hf_exception(exc) from exc
        return self._models[name]

    async def score(  # type: ignore[override]
        self,
        *,
        model: str,
        query: str,
        documents: list[str],
        batch_size: int = 32,
    ) -> list[float]:
        if not documents:
            return []
        if batch_size <= 0:
            raise BadRequestError(
                f"batch_size must be > 0, got {batch_size!r}"
            )

        encoder = await self._get_model(model)
        pairs = [(query, doc) for doc in documents]

        async with await self._rate_limiter.acquire(
            self._rate_limit_key, max_concurrency=self._max_concurrency,
        ):
            try:
                scores = await asyncio.to_thread(
                    _predict_sync, encoder, pairs, batch_size
                )
            except Exception as exc:  # noqa: BLE001
                raise _classify_hf_exception(exc) from exc

        # Defensive: ensure the predictor returned one score per pair.
        if len(scores) != len(documents):
            raise ProviderError(
                f"cross-encoder returned {len(scores)} scores for "
                f"{len(documents)} pairs"
            )
        return [float(s) for s in scores]


__all__ = ["HuggingFaceCrossEncoder"]
