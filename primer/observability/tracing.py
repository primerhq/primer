"""OTEL tracing setup + auto-instrumentation for the Matrix API.

Usage
-----
Call ``setup(config)`` once during application lifespan startup.
Use ``get_tracer(name)`` to obtain a named :class:`opentelemetry.trace.Tracer`
for custom spans in application code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

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
            exporter = OTLPSpanExporter(
                endpoint=config.otlp_endpoint,
                headers=config.otlp_headers or {},
            )
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


__all__ = ["setup", "get_tracer"]
