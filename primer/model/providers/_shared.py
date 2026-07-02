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
            "cost of killing slower generations. This is the per-event STALL "
            "timeout during streaming; it is distinct from "
            "``connect_timeout_seconds`` below, which bounds opening the stream."
        ),
    )
    connect_timeout_seconds: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Timeout in seconds for ESTABLISHING the provider response after a "
            "concurrency slot has been acquired -- i.e. opening the stream / "
            "receiving the first bytes, which on a just-in-time backend includes "
            "a COLD MODEL LOAD. ``None`` (the default) means no connect bound: a "
            "slow cold load is never aborted, it waits as long as the upstream "
            "needs. Set a value to fail fast when a held slot's upstream never "
            "begins responding (e.g. a dead endpoint). Distinct from "
            "``request_timeout_seconds`` (the per-event stall timeout during "
            "streaming). Honoured by LLM and embedding adapters that open a "
            "network request; not applicable to local (in-process) backends."
        ),
    )
    total_timeout_seconds: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Wall-clock ceiling in seconds for ONE full streamed generation, "
            "measured from the first wait after the stream opens. ``None`` "
            "(the default) means no ceiling. This is the RUNAWAY-GENERATION "
            "backstop: a model that keeps emitting tokens forever (e.g. a "
            "small local model free-running on a structured-output request) "
            "never trips the per-event stall timeout because events keep "
            "arriving -- this cap aborts such a generation cleanly, failing "
            "the turn and releasing the concurrency slot. Set it comfortably "
            "above your slowest legitimate generation. The three timeouts "
            "compose: ``connect_timeout_seconds`` bounds opening the stream "
            "(incl. cold model load), ``request_timeout_seconds`` bounds each "
            "gap between events, and this bounds the whole generation."
        ),
    )
