"""Typer CLI: ``primer api [--no-worker]`` and ``primer worker``.

Both entrypoints load (or auto-discover) a YAML config file that
populates :class:`primer.api.config.AppConfig`. The CLI is the
single place that configures stdlib logging — library code never
touches it.

Layout
------
* ``primer api`` — serve the HTTP API AND start an in-process worker
  pool. With no flags, auto-loads ``~/.primer/config.yaml`` if
  present, otherwise runs with built-in defaults (embedded SQLite at
  ``~/.primer/db/data.sqlite``).
* ``primer api --config path/to/config.yaml`` — explicit config.
* ``primer api --no-worker`` — serve the API only; the worker pool is
  expected to run in a separate ``primer worker`` process.
* ``primer worker`` — run the worker pool. A minimal HTTP surface
  (``/v1/health`` and ``/v1/workers``) is still served for
  liveness/readiness probes.

YAML schema mirrors :class:`AppConfig`. Every field is optional;
omit ``db`` entirely for the zero-config SQLite default. Example::

    db:
      provider: sqlite
      config:
        path: ~/.primer/db/data.sqlite

    scheduler:
      provider: in_memory
      config: {}

    log_level: info

Env vars (``PRIMER_*``) override missing fields; CLI YAML wins
over env vars (init args > env in pydantic-settings priority).
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
from pathlib import Path
from typing import Any

import typer
import uvicorn
import yaml
from pydantic import ValidationError

from primer.api.app import create_app, _build_storage_provider
from primer.api.config import UVICORN_GRACEFUL_SHUTDOWN_SECONDS, AppConfig
from primer.common.log import configure_logging
from primer.model.scheduler import RuntimeMode


app = typer.Typer(
    add_completion=False,
    help="Primer microagents framework — API + worker entrypoints.",
)


_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


_DEFAULT_HOME_YAML = Path("~/.primer/config.yaml")


def _resolve_config_path(explicit: Path | None) -> Path | None:
    """Pick the YAML to load.

    Priority: explicit ``--config`` > ``~/.primer/config.yaml`` if
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
            try:
                loaded = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                # One line: the rich error panel wraps long messages and
                # would split the "is not valid YAML" phrase across a
                # line break (the first-touch pin reads the raw text).
                raise typer.BadParameter(
                    f"{resolved} is not valid YAML: "
                    f"{str(exc).replace(chr(10), ' ')}"
                ) from exc
        if loaded is None:
            data = {}
        elif isinstance(loaded, dict):
            data = loaded
        else:
            raise typer.BadParameter(
                f"config file {resolved} must contain a YAML mapping at the root"
            )
    # The legacy top-level 'vector_store' block, if the file still carries
    # it. Captured here (into the narrow legacy_vector_store field) so
    # bootstrap can translate it into a persisted pgvector provider row and
    # warn, instead of AppConfig silently dropping it. Vector backends are
    # persisted SemanticSearchProvider rows: bootstrap seeds the reserved
    # pgvector provider when the app runs against Postgres and a Lance
    # provider when the 'lance' extra is installed.
    if "vector_store" in data:
        data["legacy_vector_store"] = data.pop("vector_store")
        typer.secho(
            f"config file {resolved}: the top-level `vector_store` key is "
            "legacy. It is being migrated: at startup bootstrap translates "
            "it into the pgvector semantic-search provider row (and logs a "
            "deprecation WARNING). Vector backends are now persisted "
            "provider rows (SemanticSearchProvider); remove the block from "
            "your config and register providers via POST /v1/ssp instead.",
            err=True,
            fg=typer.colors.YELLOW,
        )
    # CLI-chosen runtime_mode always wins.
    data["runtime_mode"] = runtime_mode.value
    try:
        return AppConfig(**data)
    except ValidationError as exc:
        first = "; ".join(
            ":".join(str(part) for part in err["loc"]) + ": " + err["msg"]
            for err in exc.errors()[:3]
        )
        source = f"{resolved}" if resolved is not None else "the config (file/env)"
        raise typer.BadParameter(
            f"{source} failed to validate: {first}"
        ) from exc


def _apply_logging(config: AppConfig) -> None:
    configure_logging(
        level=_LEVEL_MAP[config.log_level],
        json_format=config.log_json,
        file_path=config.log_file,
    )


def _preflight_server(config: AppConfig, config_path: Path | None) -> None:
    """Fail fast, with a one-line hint, on the two first-touch failures
    that would otherwise print a raw traceback or a bare uvicorn error:
    the bind port is already taken, or the configured Postgres is not
    reachable. Both check what the server is about to do (bind the same
    host:port, connect to the same Postgres) before it does it."""
    # SO_REUSEADDR, like uvicorn's own listener: a bind without it fails
    # against TIME_WAIT residue of a dead server (the e2e restart path:
    # the old process is gone, the kernel keeps the port ~60 s) even
    # though the real listener would start fine. With it, the probe
    # succeeds on residue-only and still raises EADDRINUSE against a
    # live listener - the semantics the refusal needs.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((config.host, config.port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            where = f" in {config_path}" if config_path is not None else ""
            raise typer.BadParameter(
                f"port {config.port} is already in use on {config.host}; "
                f"free it, or change the port: set `port:`{where} "
                "(env: PRIMER_PORT)"
            ) from exc
    finally:
        probe.close()
    if config.db is not None and config.db.provider.value == "postgres":
        pg = config.db.config
        try:
            with socket.create_connection((pg.hostname, pg.port), timeout=5):
                pass
        except OSError as exc:
            where = f" in {config_path}" if config_path is not None else " (or the PRIMER_DB__* env vars)"
            raise typer.BadParameter(
                f"Postgres at {pg.hostname}:{pg.port} is not reachable "
                f"({exc}); check the db.config block{where}: host, port, "
                "and that the server is running"
            ) from exc


def _run_uvicorn(config: AppConfig) -> None:  # pragma: no cover
    """Build the FastAPI app and hand it to uvicorn."""
    app_obj = create_app(config)
    uvicorn.run(
        app_obj,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        timeout_graceful_shutdown=UVICORN_GRACEFUL_SHUTDOWN_SECONDS,
    )


@app.command("api")
def run_api(
    config: Path | None = typer.Option(  # noqa: B008
        None,
        "--config", "-c",
        help=(
            "Path to a YAML config file. When omitted, "
            "~/.primer/config.yaml is auto-loaded if it exists; "
            "otherwise built-in defaults apply (embedded SQLite "
            "at ~/.primer/db/data.sqlite)."
        ),
        dir_okay=False, readable=True,
    ),
    no_worker: bool = typer.Option(
        False, "--no-worker",
        help=(
            "Serve the API only; do NOT start the in-process worker pool. "
            "Default is api+worker (single-process) — pass this when the "
            "worker is running in a separate `primer worker` process."
        ),
    ),
) -> None:
    """Serve the HTTP API (and an in-process worker by default)."""
    mode = RuntimeMode.API if no_worker else RuntimeMode.API_PLUS_WORKER
    cfg = _load_config(config, mode)
    _apply_logging(cfg)
    _preflight_server(cfg, config)
    _run_uvicorn(cfg)


@app.command("worker")
def run_worker(
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", "-c",
        help=(
            "Path to a YAML config file. When omitted, "
            "~/.primer/config.yaml is auto-loaded if it exists; "
            "otherwise built-in defaults apply."
        ),
        dir_okay=False, readable=True,
    ),
) -> None:
    """Run the worker pool (with a minimal health/workers HTTP surface)."""
    cfg = _load_config(config, RuntimeMode.WORKER)
    _apply_logging(cfg)
    _preflight_server(cfg, config)
    _run_uvicorn(cfg)


@app.command("init")
def run_init(
    config: Path | None = typer.Option(  # noqa: B008
        None,
        "--config", "-c",
        help=(
            "Path to a YAML config file. When omitted, "
            "~/.primer/config.yaml is auto-loaded if it exists; "
            "otherwise built-in defaults apply."
        ),
        dir_okay=False, readable=True,
    ),
    force: bool = typer.Option(
        False, "--force",
        help=(
            "Re-run bootstrap even if it has already completed. "
            "Skips rows that already exist (idempotent), but ignores "
            "the completion marker so partially-failed runs can be retried."
        ),
    ),
) -> None:
    """Run first-time bootstrap. Idempotent; --force re-runs even if completed."""
    # Use API runtime_mode — we only need the storage layer.
    cfg = _load_config(config, RuntimeMode.API)

    async def _run() -> None:
        from primer.bootstrap.runner import BootstrapRunner
        from primer.model.provider import (
            CrossEncoderProvider,
            EmbeddingProvider,
            SemanticSearchProvider,
        )
        from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate

        from primer.storage.migrations import prepare_storage

        storage_provider = _build_storage_provider(cfg)
        await storage_provider.initialize()
        try:
            # Baseline (or migrate) exactly as the API's first boot does, so that boot has nothing to replay (GR-8).
            await prepare_storage(storage_provider)
            root_dir = Path("~/.primer").expanduser()
            runner = BootstrapRunner(
                storage=storage_provider,
                embedder_storage=storage_provider.get_storage(EmbeddingProvider),
                ssp_storage=storage_provider.get_storage(SemanticSearchProvider),
                cross_encoder_storage=storage_provider.get_storage(
                    CrossEncoderProvider
                ),
                workspace_provider_storage=storage_provider.get_storage(
                    WorkspaceProvider
                ),
                workspace_template_storage=storage_provider.get_storage(
                    WorkspaceTemplate
                ),
                root_dir=root_dir,
                db_config=cfg.db,
                legacy_vector_store=cfg.legacy_vector_store,
            )
            result = await runner.run(force=force)
        finally:
            await storage_provider.aclose()

        if result.created:
            typer.echo(f"Created: {', '.join(result.created)}")
        if result.skipped:
            typer.echo(f"Skipped (already present): {', '.join(result.skipped)}")
        if result.errors:
            for provider_id, reason in result.errors:
                typer.echo(f"Error [{provider_id}]: {reason}", err=True)
            raise typer.Exit(code=1)

    asyncio.run(_run())


def main() -> None:  # pragma: no cover
    """Console-script entrypoint (see pyproject [project.scripts])."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]
