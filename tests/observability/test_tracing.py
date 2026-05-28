"""Tests for primer.observability.tracing."""

from __future__ import annotations

import pytest

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider

from primer.api.config import ObservabilityConfig
import primer.observability.tracing as tracing_module
from primer.observability.tracing import get_tracer, setup


def _reset_tracing() -> None:
    """Reset the module-level provider so tests are independent.

    Only clears the Primer-level reference; avoids touching OTEL internals
    that vary across SDK versions.
    """
    tracing_module._provider = None


class TestSetupNoOp:
    def test_disabled_enabled_flag_is_noop(self) -> None:
        _reset_tracing()
        cfg = ObservabilityConfig(enabled=False)
        setup(cfg)
        # _provider must stay None — no TracerProvider was installed.
        assert tracing_module._provider is None

    def test_traces_disabled_flag_is_noop(self) -> None:
        _reset_tracing()
        cfg = ObservabilityConfig(traces_enabled=False)
        setup(cfg)
        assert tracing_module._provider is None

    def test_setup_does_not_crash_without_otlp_endpoint(self) -> None:
        _reset_tracing()
        cfg = ObservabilityConfig(otlp_endpoint=None)
        # Should not raise.
        setup(cfg)
        assert tracing_module._provider is not None

    def test_provider_is_tracer_provider_instance(self) -> None:
        _reset_tracing()
        cfg = ObservabilityConfig(otlp_endpoint=None)
        setup(cfg)
        assert isinstance(tracing_module._provider, TracerProvider)


class TestSetupWithOtlpEndpoint:
    def test_setup_with_otlp_endpoint_does_not_crash(self) -> None:
        """setup() with a bogus OTLP endpoint must not raise.

        The exporter is constructed but no connection attempt is made until
        the first span flush.  The test verifies the setup path is safe.
        """
        _reset_tracing()
        cfg = ObservabilityConfig(
            otlp_endpoint="http://localhost:4317",
            otlp_headers={"Authorization": "Bearer test"},
        )
        # Should not raise — the exporter does not connect eagerly.
        setup(cfg)
        assert tracing_module._provider is not None

    def test_resource_attributes_applied(self) -> None:
        _reset_tracing()
        cfg = ObservabilityConfig(
            service_name="test-svc",
            service_namespace="test-ns",
        )
        setup(cfg)
        provider = tracing_module._provider
        assert provider is not None
        attrs = provider.resource.attributes
        assert attrs.get("service.name") == "test-svc"
        assert attrs.get("service.namespace") == "test-ns"


class TestGetTracer:
    def test_get_tracer_returns_tracer_instance(self) -> None:
        _reset_tracing()
        t = get_tracer("primer.test")
        assert isinstance(t, otel_trace.Tracer)

    def test_get_tracer_after_setup_returns_tracer(self) -> None:
        _reset_tracing()
        cfg = ObservabilityConfig()
        setup(cfg)
        t = get_tracer("primer.observability.test")
        assert isinstance(t, otel_trace.Tracer)

    def test_get_tracer_can_start_span(self) -> None:
        _reset_tracing()
        cfg = ObservabilityConfig()
        setup(cfg)
        tracer = get_tracer("primer.test")
        with tracer.start_as_current_span("test-span") as span:
            span.set_attribute("test.key", "value")
            # Should not raise.
            assert span is not None

    def test_get_tracer_without_setup_does_not_crash(self) -> None:
        _reset_tracing()
        # No setup() called — falls back to the OTEL global no-op proxy.
        t = get_tracer("primer.no_setup")
        assert isinstance(t, otel_trace.Tracer)
