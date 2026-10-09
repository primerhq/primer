"""Project-wide exception hierarchy.

Compact hierarchy with room to grow. Every exception inherits from
:class:`PrimerError` so callers can catch the project's errors as a
single category. Each exception carries optional ``code``,
``status_code``, and ``cause`` so adapters can plumb provider-specific
context without losing the underlying traceback.
"""

from __future__ import annotations

from primer.common.error_codes import safe_code


class PrimerError(Exception):
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
        # A provider can send any text as its error code (the OpenAI SDK's, an OpenResponses error event's); an identifier survives, anything else
        # is no code (security ticket 01a11fbc-ffea). ``str(self)`` and every sink that reads the code see only the safe value.
        self.code = safe_code(code)
        self.status_code = status_code
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        prefix_parts: list[str] = []
        if self.status_code is not None:
            prefix_parts.append(str(self.status_code))
        if self.code is not None:
            prefix_parts.append(str(self.code))
        prefix = f"[{' '.join(prefix_parts)}] " if prefix_parts else ""
        return f"{prefix}{self.message}"


class ConfigError(PrimerError):
    """Programmer or setup error — invalid configuration or arguments."""


class ContextOverflowUnrecoverable(ConfigError):
    """The prompt does not fit the model's context window and compaction cannot make it fit.

    Raised by the executor when a turn was rejected as a context overflow and recovery cannot help, for one
    of three reasons. The forced compaction found nothing it could shrink (the fixed part of the prompt, the
    system prompt plus the tool schemas, or the input the model has not answered, already fills the window):
    replaying the byte-identical prompt would fail the same way. Or the replay after a compaction was rejected
    too. Or, decided before any compaction, the agent's own ``max_output_tokens`` is not below the model's
    context window, so no history, however short, fits beside that cap (``forced_compaction`` and
    ``replay_attempted`` are both false then). A rejection whose own numbers show that the cap does not fit is
    classified an output-cap error and never gets here: it stays the provider's own rejection
    (a plain ``BadRequestError``, problem type ``/errors/bad-request``, when the adapter raised it).
    ``code`` is ``context_overflow_unrecoverable``. ``__cause__`` is the provider's rejection.
    """

    CODE = "context_overflow_unrecoverable"

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        forced_compaction: bool = False,
        replay_attempted: bool = False,
        persisted_rounds: int = 0,
        summarised_rounds: int = 0,
    ) -> None:
        super().__init__(message, code=self.CODE, cause=cause)
        self.forced_compaction = forced_compaction
        self.replay_attempted = replay_attempted
        self.persisted_rounds = persisted_rounds
        self.summarised_rounds = summarised_rounds

    @property
    def ended_detail_code(self) -> str:
        """The terminal detail the session records when this ends the turn."""
        return self.CODE

    @property
    def problem_extensions(self) -> dict[str, object]:
        """What the ERROR record and the problem details say about how far recovery got:
        whether a forced compaction ran, whether the turn was replayed after it, how many
        completed tool rounds of this turn are in the history as messages (``persisted_rounds``) and
        how many only as part of the compaction's summary (``summarised_rounds``): either way the
        next turn does not run them again."""
        return {
            "forced_compaction": self.forced_compaction,
            "replay_attempted": self.replay_attempted,
            "persisted_rounds": self.persisted_rounds,
            "summarised_rounds": self.summarised_rounds,
        }


class SummariserOverflow(ContextOverflowUnrecoverable):
    """The compaction's OWN summariser call was rejected as too large, and its input could not be reduced to fit.

    The summariser is a model call like any other and is sent the head the compaction replaces; a head
    over the model's window makes that call overflow. The compaction first retries once, text only, with
    the input reduced (tool results left out, then a bounded rolling fold, then single units cut); this is
    what is raised when that is not possible or the retry overflows too. It names the summariser so it is
    not read as the turn's own prompt not fitting. A :class:`ContextOverflowUnrecoverable`, so the same
    problem type maps it and a handler that catches that catches this; ``code`` and ``ended_detail`` are
    ``summariser_overflow``.
    """

    CODE = "summariser_overflow"

    @property
    def problem_extensions(self) -> dict[str, object]:
        """``code`` says which overflow this is (the problem type is the turn's own, shared), and the turn-recovery
        fields only when a turn's forced compaction ran: a manual compaction, or a proactive one ahead of the turn's
        call, has no recovery to describe, and four zeros there read as a statement about something that never ran."""
        extensions: dict[str, object] = {"code": self.code}
        if self.forced_compaction:
            extensions.update(super().problem_extensions)
        return extensions


class ModelNotFoundError(ConfigError):
    """Requested model isn't in the adapter's declared models list."""


class UnsupportedContentError(PrimerError):
    """Adapter cannot transmit this Part type to the provider.

    Examples: AudioPart sent to Anthropic chat (Anthropic doesn't accept
    audio); ImagePart sent to OpenAI embeddings (OpenAI embeddings are
    text-only); DocumentPart sent to Ollama (no document surface).
    """


class ValidationError(PrimerError):
    """Request was structurally valid but failed semantic validation.

    Maps to HTTP 422 (Unprocessable Entity) at the API surface. Use for
    binding-level checks that go beyond Pydantic's structural validation
    -- e.g. the request references an entity id that does not exist, or
    a discriminated-union member fails a cross-field invariant. Distinct
    from :class:`BadRequestError`, which maps to 400 and signals a
    malformed request the server could not parse or interpret.
    """


class DimensionMismatchError(ValidationError):
    """Embedder output dimensionality does not match the collection's stored dim.

    Maps to HTTP 422. Raised BEFORE embedding work begins so that CPU/
    network time is not wasted on a batch that cannot be stored. The
    error message names both dimensions and provides a re-index hint.

    Attributes
    ----------
    embedder_dim
        Dimension reported by the active embedder (probe output).
    collection_dim
        Dimension recorded in the vector store for this collection.
    collection_id
        Identifier of the mismatched collection.
    """

    def __init__(
        self,
        message: str,
        *,
        embedder_dim: int,
        collection_dim: int,
        collection_id: str,
        code: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            status_code=422,
            cause=cause,
        )
        self.embedder_dim = embedder_dim
        self.collection_dim = collection_dim
        self.collection_id = collection_id


class ProviderError(PrimerError):
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


class ToolsetUnreachableError(BadRequestError):
    """Create of an MCP toolset was blocked because its endpoint is unreachable.

    Raised by the ``POST /v1/toolsets`` pre-create connectivity probe when a
    network (http) MCP endpoint cannot be reached. Serialised as HTTP 400 with
    problem ``type == "/errors/toolset-unreachable"`` so the Console can offer
    a "Create anyway" action (re-POST with ``?allow_unreachable=true``, which
    skips the probe). Distinct from :class:`AuthRequiredError` (endpoint is
    reachable but needs OAuth) and :class:`ConfigError` (caller supplied an
    invalid config) -- both of those bubble as their own envelopes.
    """


class ServerError(ProviderError):
    """Provider encountered an internal error (5xx)."""


class ProviderTimeoutError(ProviderError):
    """LLM stream stalled: no event received within the configured window.

    Raised by adapters when ``Limits.request_timeout_seconds`` expires
    without a new event arriving from the upstream provider. The turn
    fails cleanly (the concurrency slot is released) so the worker can
    accept the next queued request. Callers that want to distinguish a
    stall from an ordinary provider error can catch this subclass
    specifically; catching :class:`ProviderError` is also sufficient.
    """


class NetworkError(PrimerError):
    """Network-level failure -- connection refused, DNS failure, timeout.

    Distinct from :class:`ProviderError` because no response was received;
    the failure is below the application protocol layer.
    """


class WorkspaceUnreachableError(PrimerError):
    """A session's workspace exists, but its files cannot be read right now.

    Its runtime does not answer, a connection broke, a mount is gone. Distinct from :class:`NotFoundError` (the file is not there: the
    session has written no log yet, which is a normal empty log) and from ``WorkspaceRefusedError`` (the deployment refuses the
    workspace on purpose). The data is presumably intact and a retry may work, so the API answers 503 and no reader of a session's log may
    treat this as an empty log.
    """


class NotFoundError(PrimerError):
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


class ConflictError(PrimerError):
    """Storage operation conflicts with the current state.

    Typical cases: :meth:`primer.int.Storage.create` when an entity
    with the same id already exists; optimistic-concurrency mismatch
    on update for backends that implement it.
    """


class AuthRequiredError(PrimerError):
    """OAuth consent required before this provider can serve requests.

    Distinct from :class:`AuthenticationError` -- that signals "we tried
    and the credentials were rejected"; this signals "the caller hasn't
    authenticated yet and the user must consent." Callers MUST handle
    this case explicitly (catch ``AuthRequiredError`` *before* any
    generic ``except PrimerError``) so the URL reaches the end user.

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


class TransientError(PrimerError):
    """Retryable failure raised by adapters (network blips, 5xx, etc.).

    The worker pool's transient-failure path catches this, applies
    exponential backoff via the scheduler, and re-enqueues the
    session. Adapters that know a failure is recoverable should raise
    this rather than a bare :class:`Exception`.

    See docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md
    for the full background-execution design.
    """


class TokenCounterUnavailable(PrimerError):
    """A token counter cannot produce a count right now.

    Raised by the counters themselves (a missing or corrupt tokenizer
    vocabulary, a tokenizer the host does not have, a counter queue that did
    not start in time, no aggregated member that can count). It is never a
    failure of the turn: ``primer.llm.counting.count_prompt_tokens`` is the one
    place that turns it into a labelled estimate. Counters must raise this (or
    a mapped provider error) rather than quietly return a heuristic number,
    because a heuristic returned as a count would be labelled native.

    ``transient`` says whether retrying later can help: ``False`` for a missing
    vocabulary (it will not appear by itself), ``True`` for a queue wait that
    timed out. The wrapper only negative-caches transient failures.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.transient = transient


class LeaseLostError(PrimerError):
    """Internal: the scheduler detected a lost lease on session release.

    The worker discards the in-progress turn output. Never escapes the
    worker boundary -- REST callers do not see this.
    """


class TurnConflictError(PrimerError):
    """Internal: the scheduler detected a turn-number conflict on release.

    Another worker advanced the session ahead of us. The worker
    discards the in-progress turn output. Same scope as
    :class:`LeaseLostError`.
    """


class ListenConnectionLost(PrimerError, ConnectionError):
    """Internal: a LISTEN connection was lost (a Postgres restart or failover,
    a network blip) while a watcher was parked on it.

    Raised out of a LISTEN-backed watcher (``ClaimEngine.watch_ready`` on
    Postgres) once every notification received before the loss has been
    delivered, so the consumer can re-subscribe. Never escapes the worker
    boundary.

    It is deliberately also a builtin :class:`ConnectionError`. That subclass
    relationship is NOT how a consumer tells it apart from a connect failure
    (a refused connection while the server is still down is also a
    ``ConnectionError``); catch this class FIRST, by name, to report "a live
    connection was lost" separately from "could not connect".
    """


class SubprocessTimeoutError(PrimerError):
    """A git or init-command subprocess exceeded the configured deadline.

    Raised by :class:`primer.workspace.local.state.LocalStateRepo` and
    :class:`primer.workspace.local.backend.LocalWorkspaceBackend` when a
    ``git`` or ``init_command`` subprocess does not complete within
    ``AppConfig.subprocess_timeout_seconds``.  The subprocess is killed
    before this error is raised so the ``.git/index.lock`` commit lock is
    always released.

    Callers that want to distinguish a subprocess stall from other workspace
    errors can catch this subclass specifically; catching
    :class:`PrimerError` is also sufficient.
    """
