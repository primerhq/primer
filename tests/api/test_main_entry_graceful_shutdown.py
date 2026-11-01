"""CV-3: both entry points start uvicorn with a bound on graceful shutdown.

The console holds an SSE tap open by design; uvicorn waits for every open connection before the lifespan shutdown (worker drain, lease release,
deferred flushes) runs, so without a bound a SIGTERM with a console open reaches the orchestrator's kill first.
"""

from __future__ import annotations

from unittest import mock

from primer.api.config import UVICORN_GRACEFUL_SHUTDOWN_SECONDS


def test_the_bound_is_short_of_a_lease_ttl_and_of_systemds_default_stop_timeout() -> None:
    from primer.model.scheduler import WorkerConfig

    assert 0 < UVICORN_GRACEFUL_SHUTDOWN_SECONDS < WorkerConfig().lease_ttl_seconds < 90


def test_python_m_primer_api_passes_it() -> None:
    import primer.api.__main__ as entry

    seen: dict = {}
    with mock.patch.object(entry, "create_app", return_value=object()), mock.patch.object(entry, "configure_logging"), \
         mock.patch.object(entry.uvicorn, "run", side_effect=lambda app, **kw: seen.update(kw)), mock.patch.object(entry, "AppConfig") as cfg:
        cfg.return_value = mock.Mock(log_level="info", log_json=False, log_file=None, host="127.0.0.1", port=1)
        entry.main()
    assert seen["timeout_graceful_shutdown"] == UVICORN_GRACEFUL_SHUTDOWN_SECONDS


def test_primer_api_and_primer_worker_pass_it() -> None:
    import primer.cli as cli

    seen: dict = {}
    with mock.patch.object(cli, "create_app", return_value=object()), \
         mock.patch.object(cli.uvicorn, "run", side_effect=lambda app, **kw: seen.update(kw)):
        cli._run_uvicorn(mock.Mock(host="127.0.0.1", port=1, log_level="info"))
    assert seen["timeout_graceful_shutdown"] == UVICORN_GRACEFUL_SHUTDOWN_SECONDS
