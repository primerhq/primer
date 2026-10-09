"""OTEL tracing setup + auto-instrumentation for the Primer API.

Usage
-----
Call ``setup(config)`` once during application lifespan startup.
Use ``get_tracer(name)`` to obtain a named :class:`opentelemetry.trace.Tracer`
for custom spans in application code.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from primer.common.log import redact_credentials

if TYPE_CHECKING:
    from primer.api.config import ObservabilityConfig

logger = logging.getLogger(__name__)

# Module-level provider reference so get_tracer() always routes to the
# same provider regardless of whether setup() has been called.
_provider: TracerProvider | None = None


def setup(config: "ObservabilityConfig") -> None:
    """Wire the OTEL TracerProvider and install auto-instrumentors.

    Safe to call multiple times (later calls update the module-level
    provider reference, useful in tests that reconfigure between runs).

    When ``config.enabled`` or ``config.traces_enabled`` is *False* the
    function is a no-op — the global OTEL provider is left as the default
    SDK no-op proxy.

    When ``config.otlp_endpoint`` is set a
    :class:`~opentelemetry.exporter.otlp.proto.grpc.OTLPSpanExporter` is
    attached.  When it is *None* spans are still recorded in-process (the
    provider is wired) but nothing is exported — useful for testing and
    for deployments that pull metrics only via Prometheus.
    """
    global _provider  # noqa: PLW0603

    if not config.enabled or not config.traces_enabled:
        logger.debug("tracing disabled via config; skipping setup")
        return

    resource = Resource.create({
        "service.name": config.service_name,
        "service.namespace": config.service_namespace,
    })

    provider = TracerProvider(resource=resource)

    if config.otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from primer.observability.span_redaction import RedactingSpanExporter

            # Every finished span crosses the wrapper once, whichever code made it: the auto-instrumentors' URLs, routes and exceptions carry credentials
            # that the spans we open ourselves (``span``) never see (ticket 01a12171-2d5a).
            exporter = RedactingSpanExporter(OTLPSpanExporter(
                endpoint=config.otlp_endpoint,
                headers=config.otlp_headers or {},
            ))
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(
                "tracing: OTLP exporter wired to %s", config.otlp_endpoint
            )
        except Exception:
            logger.exception(
                "tracing: failed to wire OTLP exporter; traces will not be exported"
            )

    trace.set_tracer_provider(provider)
    _provider = provider

    # --- Auto-instrumentation -------------------------------------------
    # Each instrumentor is installed guarded: a failure to instrument one
    # library must not prevent the others from loading.
    _install_auto_instrumentors()


def _install_auto_instrumentors() -> None:
    """Install FastAPI, asyncpg, and httpx auto-instrumentors."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor().instrument()
        logger.debug("tracing: FastAPIInstrumentor installed")
    except Exception:
        logger.exception("tracing: FastAPIInstrumentor install failed")

    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        AsyncPGInstrumentor().instrument()
        logger.debug("tracing: AsyncPGInstrumentor installed")
    except Exception:
        logger.exception("tracing: AsyncPGInstrumentor install failed")

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        logger.debug("tracing: HTTPXClientInstrumentor installed")
    except Exception:
        logger.exception("tracing: HTTPXClientInstrumentor install failed")


def get_tracer(name: str) -> trace.Tracer:
    """Return a named OTEL :class:`~opentelemetry.trace.Tracer`.

    Uses the module-level provider if :func:`setup` has been called;
    falls back to the OTEL global (which may be a no-op proxy if setup
    was never called, e.g. in unit tests that do not boot the full app).
    """
    if _provider is not None:
        return _provider.get_tracer(name)
    # Fall back to the OTEL global — this is the ProxyTracerProvider or the
    # SDK no-op provider; it always supports get_tracer().
    return trace.get_tracer(name)


def record_failure(span: trace.Span, exc: BaseException) -> None:
    """Record ``exc`` on ``span`` the way an OTLP exporter may send it: the type, and the message with credentials masked.

    ``Span.record_exception`` exports the raw message AND a stacktrace that ends in the same message, and ``start_as_current_span`` records an escaping
    exception that way on its own. A tool's exception text carries what a library printed (httpx prints a request URL whole, ``user:password@`` and
    ``?api_key=`` included; an ``Authorization`` header is echoed back), so the message goes through :func:`redact_credentials` (URL credentials, Bearer
    and Basic tokens) and no stacktrace is recorded (security ticket 01a1201c-8918). A credential-free message is recorded as it was.

    The type is named as the SDK names it: the bare qualname for a builtin, ``module.qualname`` otherwise. A span that is not recording (tracing off, or
    sampled out) records nothing, and the message is not rendered.
    """
    if not span.is_recording():
        return
    try:
        message = redact_credentials(str(exc))
    except Exception:  # noqa: BLE001 - a raising __str__ must not turn a span record into a second failure
        message = ""
    cls = type(exc)
    type_name = cls.__qualname__ if cls.__module__ in ("builtins", None) else f"{cls.__module__}.{cls.__qualname__}"
    span.add_event("exception", {"exception.type": type_name, "exception.message": message})
    span.set_status(Status(StatusCode.ERROR, f"{cls.__name__}: {message}" if message else cls.__name__))


@contextmanager
def span(tracer: trace.Tracer, name: str) -> Iterator[trace.Span]:
    """``tracer.start_as_current_span(name)`` that records a failure with :func:`record_failure` instead of the SDK's own recording.

    An ``Exception`` that leaves the block is recorded (type, masked message, ERROR status) and re-raised; a ``BaseException`` (a cancellation) is not a
    failure of the work and is not recorded. Use it wherever the exception that can leave the block carries text from a tool, a peer or a provider.

    Use it in a ``with`` statement only: it is a plain context manager, so as a decorator on an ``async def`` it would cover the creation of the coroutine,
    not its run.
    """
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as current:
        try:
            yield current
        except Exception as exc:
            record_failure(current, exc)
            raise


__all__ = ["setup", "get_tracer", "span", "record_failure"]
