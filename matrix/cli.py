"""Typer CLI: ``matrix api [--run-worker]`` and ``matrix worker``.

Both entrypoints load a YAML config file that populates
:class:`matrix.api.config.AppConfig`. The CLI is the single place that
configures stdlib logging — library code never touches it.

Layout
------
* ``matrix api --config config.yaml`` — serve the HTTP API.
* ``matrix api --config config.yaml --run-worker`` — serve the API AND
  run the in-process worker pool (debug / single-process dev only).
* ``matrix worker --config config.yaml`` — run the worker pool. A
  minimal HTTP surface (``/v1/health`` and ``/v1/workers``) is still
  served for liveness/readiness probes and graceful drain control.

YAML config schema mirrors :class:`AppConfig`. Example::

    db_host: localhost
    db_port: 5432
    db_database: matrix
    db_user: matrix
    db_password: matrix
    log_file: ./logs/matrix.log
    log_level: info
    scheduler:
      provider: postgres
      config: {}
    vector_store:
      provider: pgvector
      config:
        hostname: localhost
        port: 5432
        database: matrix
        username: matrix
        password: matrix

Env vars (``MATRIX_*``) still override any field absent from the YAML
file — pydantic-settings handles the merge.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import typer
import uvicorn
import yaml

from matrix.api.app import create_app
from matrix.api.config import AppConfig
from matrix.common.log import configure_logging
from matrix.model.scheduler import RuntimeMode


app = typer.Typer(
    add_completion=False,
    help="Matrix microagents framework — API + worker entrypoints.",
)


_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _load_config(
    config_path: Path,
    runtime_mode: RuntimeMode,
) -> AppConfig:
    """Read the YAML config, force the runtime mode, build AppConfig."""
    if not config_path.exists():
        raise typer.BadParameter(f"config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise typer.BadParameter(
            f"config file {config_path} must contain a YAML mapping at the root"
        )
    # CLI-chosen runtime_mode always wins over whatever the YAML says.
    data["runtime_mode"] = runtime_mode.value
    return AppConfig(**data)


def _apply_logging(config: AppConfig) -> None:
    """Configure the root logger from AppConfig before anything else logs."""
    configure_logging(
        level=_LEVEL_MAP[config.log_level],
        json_format=config.log_json,
        file_path=config.log_file,
    )


def _run_uvicorn(config: AppConfig) -> None:  # pragma: no cover
    """Build the FastAPI app and hand it to uvicorn.

    Excluded from coverage because exercising it would require a live
    Postgres + a real network bind. Tests patch this symbol to capture
    the constructed AppConfig without spinning up a server.
    """
    app_obj = create_app(config)
    uvicorn.run(
        app_obj,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
    )


@app.command("api")
def run_api(
    config: Path = typer.Option(  # noqa: B008
        ...,
        "--config",
        "-c",
        help="Path to the YAML config file (populates AppConfig).",
        exists=False,  # checked in _load_config for a friendlier error
        dir_okay=False,
        readable=True,
    ),
    run_worker: bool = typer.Option(
        False,
        "--run-worker",
        help=(
            "Also start the in-process worker pool (debug / single-process "
            "dev only). Equivalent to runtime_mode=api+worker."
        ),
    ),
) -> None:
    """Serve the HTTP API."""
    mode = RuntimeMode.API_PLUS_WORKER if run_worker else RuntimeMode.API
    cfg = _load_config(config, mode)
    _apply_logging(cfg)
    _run_uvicorn(cfg)


@app.command("worker")
def run_worker(
    config: Path = typer.Option(  # noqa: B008
        ...,
        "--config",
        "-c",
        help="Path to the YAML config file (populates AppConfig).",
        exists=False,
        dir_okay=False,
        readable=True,
    ),
) -> None:
    """Run the worker pool (with a minimal health/workers HTTP surface)."""
    cfg = _load_config(config, RuntimeMode.WORKER)
    _apply_logging(cfg)
    _run_uvicorn(cfg)


def main() -> None:  # pragma: no cover
    """Console-script entrypoint (see pyproject [project.scripts])."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]
