"""TurnLogWriter ABC + three concrete implementations.

``WorkspaceTurnLogWriter`` serialises each :class:`TurnLogEvent` to a
single JSON line and hands it to an injected ``append_line`` callable
which is responsible for the actual file write. Write-through (no
buffering) because turn events are low-frequency (one per turn boundary)
and small (~500 bytes); operators benefit from immediate durability over
the negligible per-event fs cost.

``StorageTurnLogWriter`` persists ``TurnLogRecord`` rows via the standard
``Storage[T]`` ABC; the seq counter is in-memory per writer instance.
Each writer is scoped by ``(run_id, node_id)`` so the counter is
authoritative within a single graph-run lifetime.

``NoopTurnLogWriter`` is the test default - accepts every call, advances
a counter, never touches IO.

``to_problem_details(exc)`` translates a live exception into the same
:class:`ProblemDetails` shape the FastAPI error handlers produce so the
existing UI renderer just works. Mirrors ``_PRIMER_ERROR_MAP`` in
:mod:`primer.api.errors`; duplicated here so this module does not import
upward into the api layer.
"""

from __future__ import annotations

import json
import logging
import traceback
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Awaitable, Callable

from primer.model.except_ import (
    AuthenticationError,
    AuthRequiredError,
    BadRequestError,
    ConfigError,
    ConflictError,
    ModelNotFoundError,
    NetworkError,
    NotFoundError,
    PrimerError,
    ProviderError,
    RateLimitError,
    ServerError,
    UnsupportedContentError,
    ValidationError,
)
from primer.model.problem_details import ProblemDetails
from primer.model.turn_log import (
    TurnLogEvent,
    TurnLogRecord,
)


if TYPE_CHECKING:
    from primer.int.storage_provider import Storage


logger = logging.getLogger(__name__)


# Mirrors primer/api/errors.py::_PRIMER_ERROR_MAP. Order matters: more
# specific subclasses first so isinstance() walks pick the tightest fit.
_PRIMER_ERROR_MAP: list[tuple[type[PrimerError], int, str, str]] = [
    (BadRequestError, 400, "/errors/bad-request", "Bad Request"),
    (AuthenticationError, 401, "/errors/authentication-failed",
        "Authentication Failed"),
    (AuthRequiredError, 401, "/errors/auth-required",
        "Authentication Required"),
    (ModelNotFoundError, 404, "/errors/model-not-found",
        "Model Not Found"),
    (NotFoundError, 404, "/errors/not-found", "Not Found"),
    (ConflictError, 409, "/errors/conflict", "Conflict"),
    (RateLimitError, 429, "/errors/rate-limited", "Rate Limited"),
    (ValidationError, 422, "/errors/validation-error", "Validation Error"),
    (UnsupportedContentError, 422, "/errors/unsupported-content",
        "Unsupported Content"),
    (ServerError, 502, "/errors/provider-server-error",
        "Provider Server Error"),
    (ProviderError, 502, "/errors/provider-error", "Provider Error"),
    (NetworkError, 504, "/errors/network-error", "Network Error"),
    (ConfigError, 503, "/errors/service-unavailable",
        "Service Unavailable"),
    (PrimerError, 500, "/errors/internal", "Internal Error"),
]


def to_problem_details(exc: BaseException) -> ProblemDetails:
    """Translate a live exception into a ProblemDetails envelope.

    For ``PrimerError`` subclasses uses the same map the FastAPI error
    handlers use. For unknown exceptions returns a generic 500 envelope
    with the exception class name as the title. The exception class name
    and a 4 KB-tail-truncated traceback land in ``extensions``.
    """
    tb_text = _truncate_traceback(exc)
    for exc_cls, status, type_uri, title in _PRIMER_ERROR_MAP:
        if isinstance(exc, exc_cls):
            detail = exc.message if isinstance(exc, PrimerError) else str(exc)
            return ProblemDetails(
                type=type_uri,
                title=title,
                status=status,
                detail=detail,
                extensions={
                    "exception_class": type(exc).__name__,
                    "traceback": tb_text,
                },
            )
    return ProblemDetails(
        type="/errors/internal",
        title=type(exc).__name__,
        status=500,
        detail=str(exc),
        extensions={
            "exception_class": type(exc).__name__,
            "traceback": tb_text,
        },
    )


def _truncate_traceback(exc: BaseException, max_bytes: int = 4096) -> str:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > max_bytes:
        return "...[truncated]...\n" + tb[-max_bytes:]
    return tb


AppendLine = Callable[[bytes], Awaitable[None]]
ReadExisting = Callable[[], Awaitable[bytes]]


class TurnLogWriter(ABC):
    """Append one TurnLogEvent at a time; track a per-writer seq counter."""

    @abstractmethod
    async def append(self, event: TurnLogEvent) -> int:
        """Append the event; return the assigned seq."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources. Idempotent."""


class NoopTurnLogWriter(TurnLogWriter):
    """No-op writer. Default when no real writer is wired."""

    def __init__(self) -> None:
        self._seq = 0
        self._closed = False

    async def append(self, event: TurnLogEvent) -> int:
        self._seq += 1
        return self._seq

    async def aclose(self) -> None:
        self._closed = True


class WorkspaceTurnLogWriter(TurnLogWriter):
    """JSONL appender backed by an injected ``append_line`` callable.

    The callable is path-bound by the construction site (typically a
    closure that calls ``workspace.append_state_line(<rel_path>, line)``
    or similar). Tests inject a list-capturing fake.

    When a ``read_existing`` callable is supplied, the writer lazily
    bootstraps its seq counter on the first append by reading the file
    and finding ``max(seq)``. This makes the seq stream monotonic
    across worker restarts mid-session -- without it, a restart would
    write seq=1 on top of disk's existing seq space and break
    ``since_seq`` pagination.
    """

    def __init__(
        self,
        *,
        append_line: AppendLine,
        read_existing: ReadExisting | None = None,
    ) -> None:
        self._append = append_line
        self._read = read_existing
        self._seq = 0
        self._closed = False
        self._bootstrapped = read_existing is None

    async def _bootstrap(self) -> None:
        """Read existing file (if any) and seed ``_seq`` to ``max(seq)``."""
        if self._bootstrapped:
            return
        self._bootstrapped = True  # set first so an exception still pins it
        if self._read is None:
            return
        try:
            raw = await self._read()
        except Exception:  # noqa: BLE001 -- missing-file / IO / decode
            raw = b""
        if not raw:
            return
        max_seq = 0
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                seq = int(obj.get("seq", 0))
            except Exception:  # noqa: BLE001 -- bogus line
                continue
            if seq > max_seq:
                max_seq = seq
        self._seq = max_seq

    async def append(self, event: TurnLogEvent) -> int:
        if self._closed:
            raise RuntimeError("append on closed TurnLogWriter")
        await self._bootstrap()
        self._seq += 1
        event_with_seq = event.model_copy(update={"seq": self._seq})
        line = event_with_seq.model_dump_json().encode() + b"\n"
        await self._append(line)
        return self._seq

    async def aclose(self) -> None:
        self._closed = True


class StorageTurnLogWriter(TurnLogWriter):
    """Storage-backed writer for the StorageGraphExecutor variant.

    Persists ``TurnLogRecord`` rows via the standard ``Storage[T]`` ABC.
    Scoped by ``(run_id, node_id)``: each graph run gets one
    graph-level writer (node_id=None) + one per-node writer.

    Seq counter is in-memory for v1; correct for a single-process
    executor lifetime. Cross-process resume is a deferred concern.
    """

    def __init__(
        self,
        *,
        storage: "Storage[TurnLogRecord]",
        run_id: str,
        node_id: str | None = None,
    ) -> None:
        self._storage = storage
        self._run_id = run_id
        self._node_id = node_id
        self._seq = 0
        self._closed = False

    async def append(self, event: TurnLogEvent) -> int:
        if self._closed:
            raise RuntimeError("append on closed TurnLogWriter")
        self._seq += 1
        full = event.model_dump(mode="json")
        payload = {
            k: v for k, v in full.items()
            if k not in (
                "seq", "kind", "ts", "node_id", "iteration",
                "superstep_id", "turn_no",
            )
        }
        rec = TurnLogRecord(
            id=f"tlr-{self._run_id}-{self._node_id or 'graph'}-{self._seq}",
            run_id=self._run_id,
            node_id=self._node_id,
            seq=self._seq,
            kind=event.kind,
            iteration=event.iteration,
            superstep_id=event.superstep_id,
            payload=payload,
            created_at=event.ts,
        )
        await self._storage.create(rec)
        return self._seq

    async def aclose(self) -> None:
        self._closed = True


async def safe_append(writer: TurnLogWriter, event: TurnLogEvent) -> None:
    """Append `event` via `writer`; swallow + log any IO failure.

    Turn logging is best-effort observability, not a correctness primitive.
    Disk-full or other transient errors must not abort the live dispatch /
    graph executor.
    """
    try:
        await writer.append(event)
    except Exception:  # noqa: BLE001
        logger.exception(
            "turn_log append failed (kind=%s); continuing",
            getattr(event, "kind", "?"),
        )


__all__ = [
    "AppendLine",
    "NoopTurnLogWriter",
    "StorageTurnLogWriter",
    "TurnLogWriter",
    "WorkspaceTurnLogWriter",
    "safe_append",
    "to_problem_details",
]
