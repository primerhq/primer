"""Tests for ObservabilityConfig defaults and integration with AppConfig."""

from __future__ import annotations

import pytest

from primer.api.config import AppConfig, ObservabilityConfig


class TestObservabilityConfigDefaults:
    def test_enabled_default_is_true(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.enabled is True

    def test_traces_enabled_default_is_true(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.traces_enabled is True

    def test_metrics_enabled_default_is_true(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.metrics_enabled is True

    def test_trace_llm_io_default_is_false(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.trace_llm_io is False

    def test_otlp_endpoint_default_is_none(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.otlp_endpoint is None

    def test_otlp_headers_default_is_empty_dict(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.otlp_headers == {}

    def test_service_name_default(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.service_name == "matrix"

    def test_service_namespace_default(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.service_namespace == "default"

    def test_can_override_all_fields(self) -> None:
        cfg = ObservabilityConfig(
            enabled=False,
            traces_enabled=False,
            metrics_enabled=False,
            trace_llm_io=True,
            otlp_endpoint="http://otel:4317",
            otlp_headers={"Authorization": "Bearer token"},
            service_name="my-service",
            service_namespace="production",
        )
        assert cfg.enabled is False
        assert cfg.traces_enabled is False
        assert cfg.metrics_enabled is False
        assert cfg.trace_llm_io is True
        assert cfg.otlp_endpoint == "http://otel:4317"
        assert cfg.otlp_headers == {"Authorization": "Bearer token"}
        assert cfg.service_name == "my-service"
        assert cfg.service_namespace == "production"

    def test_otlp_headers_instances_are_independent(self) -> None:
        """Each ObservabilityConfig instance gets its own headers dict."""
        cfg1 = ObservabilityConfig()
        cfg2 = ObservabilityConfig()
        cfg1.otlp_headers["key"] = "val"
        assert "key" not in cfg2.otlp_headers


class TestAppConfigObservabilityIntegration:
    def test_app_config_has_observability_field(self) -> None:
        cfg = AppConfig()
        assert hasattr(cfg, "observability")
        assert isinstance(cfg.observability, ObservabilityConfig)

    def test_app_config_observability_defaults(self) -> None:
        cfg = AppConfig()
        assert cfg.observability.enabled is True
        assert cfg.observability.traces_enabled is True
        assert cfg.observability.metrics_enabled is True
        assert cfg.observability.trace_llm_io is False
        assert cfg.observability.otlp_endpoint is None

    def test_app_config_accepts_custom_observability(self) -> None:
        cfg = AppConfig(
            observability=ObservabilityConfig(
                enabled=False,
                service_name="custom",
            )
        )
        assert cfg.observability.enabled is False
        assert cfg.observability.service_name == "custom"
