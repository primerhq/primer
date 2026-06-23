"""Shared building blocks reused across multiple provider families.

These types are not tied to one provider family: ``Limits`` constrains
every model-family provider (LLM, embedding, cross-encoder), and
``_HttpApiKeyConfig`` is the common shape behind the OpenAI-compatible
HTTP configs in both the LLM and embedding families.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, PositiveInt, SecretStr


class _HttpApiKeyConfig(BaseModel):
    """Shared shape for HTTP providers authenticated by an API key.

    Not intended to be used directly; subclass it for each concrete provider
    so that type checkers can keep the providers distinct even when their
    connection fields are identical.

    ``api_key`` is optional at the schema level so operators can register
    self-hosted endpoints (LM Studio, vLLM, llama.cpp server, a sidecar
    proxy that injects auth) that don't require a bearer token. Adapters
    that talk to providers which *do* require auth surface a 401 from
    the upstream provider at call time, which is the natural place for
    that error to manifest.
    """

    url: HttpUrl = Field(
        ...,
        description="Base URL of the provider's HTTP endpoint.",
    )
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Optional API key forwarded as the Authorization bearer. "
            "Leave unset for unauthenticated endpoints (LM Studio, "
            "self-hosted vLLM, etc.); the upstream provider will return "
            "401 at call time if it actually requires authentication."
        ),
    )


class Limits(BaseModel):
    """Rate-limit settings the client must respect for a provider."""

    max_concurrency: PositiveInt = Field(
        ...,
        description="Maximum number of in-flight requests allowed at once.",
    )
    request_timeout_seconds: float | None = Field(
        default=300.0,
        ge=0.0,
        description=(
            "Per-event inactivity timeout in seconds for LLM streaming calls. "
            "If no event arrives from the provider within this window the stream "
            "is aborted and the turn fails cleanly, releasing the concurrency "
            "slot. This is a stall timeout (no new event received) -- not a "
            "total-generation cap, so long but progressing responses are not "
            "interrupted. Set to None to disable. "
            "LM Studio note: LM Studio can stall mid-generation on large models "
            "or low-memory hardware; the default 300 s covers most real runs. "
            "Lower it (e.g. 60) if you want faster failure detection at the "
            "cost of killing slower generations."
        ),
    )
