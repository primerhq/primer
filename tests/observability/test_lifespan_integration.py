"""Integration tests for the /metrics endpoint + observability lifespan wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from primer.api.app import _make_lifespan, create_app
from primer.api.config import AppConfig, ObservabilityConfig
from primer.model.scheduler import RuntimeMode


@pytest.fixture
def sqlite_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """AppConfig that uses a fresh SQLite DB in tmp_path; API-only mode."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return AppConfig(
        runtime_mode=RuntimeMode.API,
        auto_bootstrap=False,
    )


class TestMetricsEndpointEnabled:
    @pytest.mark.asyncio
    async def test_get_metrics_returns_200(
        self, sqlite_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """GET /metrics returns 200 with Prometheus text when metrics_enabled=True.

        Starlette's Mount redirects /metrics → /metrics/ with a 307; we follow
        redirects so the final response is the prometheus_client ASGI app.
        """
        cfg = sqlite_config
        assert cfg.observability.metrics_enabled is True
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=True,
            ) as client:
                response = await client.get("/metrics")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_metrics_returns_prometheus_text_content_type(
        self, sqlite_config: AppConfig
    ) -> None:
        """GET /metrics returns a Prometheus-compatible text/plain body."""
        app = create_app(sqlite_config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=True,
            ) as client:
                response = await client.get("/metrics")
        assert response.status_code == 200
        # prometheus_client sets text/plain with version + charset
        assert "text/plain" in response.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_get_metrics_body_is_prometheus_format(
        self, sqlite_config: AppConfig
    ) -> None:
        """The /metrics body is valid text (Prometheus exposition format)."""
        app = create_app(sqlite_config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=True,
            ) as client:
                response = await client.get("/metrics")
        assert response.status_code == 200
        body = response.text
        assert isinstance(body, str)


class TestMetricsEndpointDisabled:
    @pytest.mark.asyncio
    async def test_get_metrics_returns_404_when_metrics_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /metrics returns 404 when metrics_enabled=False."""
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = AppConfig(
            runtime_mode=RuntimeMode.API,
            auto_bootstrap=False,
            observability=ObservabilityConfig(metrics_enabled=False),
        )
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/metrics")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_metrics_returns_404_when_observability_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /metrics returns 404 when enabled=False (master kill-switch)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = AppConfig(
            runtime_mode=RuntimeMode.API,
            auto_bootstrap=False,
            observability=ObservabilityConfig(enabled=False),
        )
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/metrics")
        assert response.status_code == 404


class TestLifespanTracingWiring:
    @pytest.mark.asyncio
    async def test_lifespan_calls_tracing_setup(
        self, sqlite_config: AppConfig
    ) -> None:
        """Lifespan sets the module-level _provider when traces_enabled=True."""
        from primer.observability import tracing as tracing_module

        # Reset module state before the test.
        tracing_module._provider = None

        app = FastAPI(lifespan=_make_lifespan(sqlite_config))
        async with app.router.lifespan_context(app):
            # After lifespan startup, setup() should have been called.
            from opentelemetry.sdk.trace import TracerProvider
            assert isinstance(tracing_module._provider, TracerProvider)

    @pytest.mark.asyncio
    async def test_lifespan_no_tracing_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When traces_enabled=False, setup() is a no-op and _provider stays None."""
        monkeypatch.setenv("HOME", str(tmp_path))
        from primer.observability import tracing as tracing_module

        tracing_module._provider = None

        cfg = AppConfig(
            runtime_mode=RuntimeMode.API,
            auto_bootstrap=False,
            observability=ObservabilityConfig(traces_enabled=False),
        )
        app = FastAPI(lifespan=_make_lifespan(cfg))
        async with app.router.lifespan_context(app):
            assert tracing_module._provider is None


class TestS7InstrumentsAreExported:
    """The gap instruments must reach the Prometheus text, not just the
    registry: a metric declared but never exported is invisible to the
    collector scrape that delivers OTLP metrics (2026-08-15 amendment)."""

    @pytest.mark.asyncio
    async def test_metrics_text_carries_the_s7_families(
        self, sqlite_config: AppConfig
    ) -> None:
        import primer.observability.metrics as m

        m.reset_for_test()
        try:
            m.worker_tasks_total.labels("host-0", "session", "ok").inc()
            m.turns_total.labels("ag-1", "completed").inc()
            m.llm_calls_total.labels("prov-1", "prof-1", "ok").inc()
            m.llm_profile_tokens_total.labels("prof-1", "in").inc(5)
            m.sessions_active.labels("ws-1").set(2)
            m.worker_task_duration_seconds.labels(
                "host-0", "session", "ok",
            ).observe(1.0)
            m.turn_duration_seconds.labels("ag-1", "completed").observe(2.0)

            app = create_app(sqlite_config)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=True,
                ) as client:
                    response = await client.get("/metrics")
            body = response.text
            for family in (
                "worker_tasks_total",
                "worker_task_duration_seconds",
                "turns_total",
                "turn_duration_seconds",
                "llm_calls_total",
                "llm_profile_tokens_total",
                "sessions_active",
            ):
                assert f"# TYPE {family}" in body, f"{family} missing from /metrics"
            assert 'worker{' not in body  # labels render inside the family
            assert 'session_id="' not in body
        finally:
            m.reset_for_test()
