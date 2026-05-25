"""Typer CLI: ``matrix api [--no-worker]`` and ``matrix worker``.

Both entrypoints load (or auto-discover) a YAML config file that
populates :class:`matrix.api.config.AppConfig`. The CLI is the
single place that configures stdlib logging — library code never
touches it.

Layout
------
* ``matrix api`` — serve the HTTP API AND start an in-process worker
  pool. With no flags, auto-loads ``~/.matrix/config.yaml`` if
  present, otherwise runs with built-in defaults (embedded SQLite at
  ``~/.matrix/db/data.sqlite``).
* ``matrix api --config path/to/config.yaml`` — explicit config.
* ``matrix api --no-worker`` — serve the API only; the worker pool is
  expected to run in a separate ``matrix worker`` process.
* ``matrix worker`` — run the worker pool. A minimal HTTP surface
  (``/v1/health`` and ``/v1/workers``) is still served for
  liveness/readiness probes.

YAML schema mirrors :class:`AppConfig`. Every field is optional;
omit ``db`` entirely for the zero-config SQLite default. Example::

    db:
      provider: sqlite
      config:
        path: ~/.matrix/db/data.sqlite

    scheduler:
      provider: in_memory
      config: {}

    log_level: info

Env vars (``MATRIX_*``) override missing fields; CLI YAML wins
over env vars (init args > env in pydantic-settings priority).
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


_DEFAULT_HOME_YAML = Path("~/.matrix/config.yaml")


def _resolve_config_path(explicit: Path | None) -> Path | None:
    """Pick the YAML to load.

    Priority: explicit ``--config`` > ``~/.matrix/config.yaml`` if
    it exists > None (use built-in defaults).
    """
    if explicit is not None:
        return explicit
    home_yaml = _DEFAULT_HOME_YAML.expanduser()
    if home_yaml.is_file():
        return home_yaml
    return None


def _load_config(
    config_path: Path | None,
    runtime_mode: RuntimeMode,
) -> AppConfig:
    """Read the YAML config (or use built-in defaults) and build AppConfig."""
    resolved = _resolve_config_path(config_path)
    data: dict[str, Any] = {}
    if resolved is not None:
        if not resolved.exists():
            raise typer.BadParameter(f"config file not found: {resolved}")
        with resolved.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
        if loaded is None:
            data = {}
        elif isinstance(loaded, dict):
            data = loaded
        else:
            raise typer.BadParameter(
                f"config file {resolved} must contain a YAML mapping at the root"
            )
    # CLI-chosen runtime_mode always wins.
    data["runtime_mode"] = runtime_mode.value
    return AppConfig(**data)


def _apply_logging(config: AppConfig) -> None:
    configure_logging(
        level=_LEVEL_MAP[config.log_level],
        json_format=config.log_json,
        file_path=config.log_file,
    )


def _run_uvicorn(config: AppConfig) -> None:  # pragma: no cover
    """Build the FastAPI app and hand it to uvicorn."""
    app_obj = create_app(config)
    uvicorn.run(
        app_obj,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
    )


@app.command("api")
def run_api(
    config: Path | None = typer.Option(  # noqa: B008
        None,
        "--config", "-c",
        help=(
            "Path to a YAML config file. When omitted, "
            "~/.matrix/config.yaml is auto-loaded if it exists; "
            "otherwise built-in defaults apply (embedded SQLite "
            "at ~/.matrix/db/data.sqlite)."
        ),
        dir_okay=False, readable=True,
    ),
    no_worker: bool = typer.Option(
        False, "--no-worker",
        help=(
            "Serve the API only; do NOT start the in-process worker pool. "
            "Default is api+worker (single-process) — pass this when the "
            "worker is running in a separate `matrix worker` process."
        ),
    ),
) -> None:
    """Serve the HTTP API (and an in-process worker by default)."""
    mode = RuntimeMode.API if no_worker else RuntimeMode.API_PLUS_WORKER
    cfg = _load_config(config, mode)
    _apply_logging(cfg)
    _run_uvicorn(cfg)


@app.command("worker")
def run_worker(
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", "-c",
        help=(
            "Path to a YAML config file. When omitted, "
            "~/.matrix/config.yaml is auto-loaded if it exists; "
            "otherwise built-in defaults apply."
        ),
        dir_okay=False, readable=True,
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
