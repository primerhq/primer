"""Project-wide exception hierarchy.

Compact hierarchy with room to grow. Every exception inherits from
:class:`MatrixError` so callers can catch the project's errors as a
single category. Each exception carries optional ``code``,
``status_code``, and ``cause`` so adapters can plumb provider-specific
context without losing the underlying traceback.
"""

from __future__ import annotations


class MatrixError(Exception):
    """Root of the primer exception hierarchy.

    All primer-raised exceptions inherit from this class. Carries optional
    structured context: ``code`` (provider-side error code, when known),
    ``status_code`` (HTTP status, when applicable), and ``cause`` (the
    wrapped underlying exception, also set on ``__cause__`` so tracebacks
    chain naturally).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        prefix_parts: list[str] = []
        if self.status_code is not None:
            prefix_parts.append(str(self.status_code))
        if self.code is not None:
            prefix_parts.append(self.code)
        prefix = f"[{' '.join(prefix_parts)}] " if prefix_parts else ""
        return f"{prefix}{self.message}"


class ConfigError(MatrixError):
    """Programmer or setup error — invalid configuration or arguments."""


class ModelNotFoundError(ConfigError):
    """Requested model isn't in the adapter's declared models list."""


class UnsupportedContentError(MatrixError):
    """Adapter cannot transmit this Part type to the provider.

    Examples: AudioPart sent to Anthropic chat (Anthropic doesn't accept
    audio); ImagePart sent to OpenAI embeddings (OpenAI embeddings are
    text-only); DocumentPart sent to Ollama (no document surface).
    """


class ValidationError(MatrixError):
    """Request was structurally valid but failed semantic validation.

    Maps to HTTP 422 (Unprocessable Entity) at the API surface. Use for
    binding-level checks that go beyond Pydantic's structural validation
    -- e.g. the request references an entity id that does not exist, or
    a discriminated-union member fails a cross-field invariant. Distinct
    from :class:`BadRequestError`, which maps to 400 and signals a
    malformed request the server could not parse or interpret.
    """


class ProviderError(MatrixError):
    """Upstream provider returned an error.

    Base class for all errors that originate from the provider's HTTP
    response. Adapters wrap the provider SDK's exceptions into one of
    the four subclasses below based on status code or exception type.
    """


class AuthenticationError(ProviderError):
    """Provider rejected credentials (401-style)."""


class RateLimitError(ProviderError):
    """Provider rejected the request for rate-limit reasons (429-style)."""


class BadRequestError(ProviderError):
    """Provider rejected the request as malformed or invalid (400-style)."""


class ServerError(ProviderError):
    """Provider encountered an internal error (5xx)."""


class NetworkError(MatrixError):
    """Network-level failure — connection refused, DNS failure, timeout.

    Distinct from :class:`ProviderError` because no response was received;
    the failure is below the application protocol layer.
    """


class NotFoundError(MatrixError):
    """Storage lookup found no entity matching the request.

    Raised by :class:`primer.int.Storage` operations that target a
    specific entity (``update``, ``delete``) when the id does not
    exist. Distinct from :class:`ModelNotFoundError`, which is about
    LLM/embedding model names not being in an adapter's permitted
    models list.

    :meth:`primer.int.Storage.get` does NOT raise this -- it returns
    ``None`` for missing entities so callers can branch without
    catching exceptions.
    """


class ConflictError(MatrixError):
    """Storage operation conflicts with the current state.

    Typical cases: :meth:`primer.int.Storage.create` when an entity
    with the same id already exists; optimistic-concurrency mismatch
    on update for backends that implement it.
    """


class AuthRequiredError(MatrixError):
    """OAuth consent required before this provider can serve requests.

    Distinct from :class:`AuthenticationError` -- that signals "we tried
    and the credentials were rejected"; this signals "the caller hasn't
    authenticated yet and the user must consent." Callers MUST handle
    this case explicitly (catch ``AuthRequiredError`` *before* any
    generic ``except MatrixError``) so the URL reaches the end user.

    The ``state`` field is opaque to the application; the caller passes
    it back to :meth:`primer.toolset.mcp.McpToolsetProvider.complete_oauth`
    together with the ``code`` query parameter the OAuth server delivered
    to the redirect URI.
    """

    def __init__(
        self,
        message: str,
        *,
        auth_url: str,
        state: str,
        code: str | None = None,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=status_code, cause=cause)
        self.auth_url = auth_url
        self.state = state


class TransientError(MatrixError):
    """Retryable failure raised by adapters (network blips, 5xx, etc.).

    The worker pool's transient-failure path catches this, applies
    exponential backoff via the scheduler, and re-enqueues the
    session. Adapters that know a failure is recoverable should raise
    this rather than a bare :class:`Exception`.

    See docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md
    for the full background-execution design.
    """


class LeaseLostError(MatrixError):
    """Internal: ``Scheduler.complete_turn`` returned ``LEASE_LOST``.

    The worker discards the in-progress turn output. Never escapes the
    worker boundary -- REST callers do not see this.
    """


class TurnConflictError(MatrixError):
    """Internal: ``Scheduler.complete_turn`` returned ``TURN_CONFLICT``.

    Another worker advanced the session ahead of us. The worker
    discards the in-progress turn output. Same scope as
    :class:`LeaseLostError`.
    """
