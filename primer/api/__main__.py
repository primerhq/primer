"""Entry point: ``python -m primer.api`` runs the API with uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.common.log import configure_logging


def main() -> None:
    config = AppConfig()  # type: ignore[call-arg]
    # Same pipeline as `primer api` (primer.cli._apply_logging): without it
    # the URL-credential filter is never installed and uvicorn's access
    # line logs webhook capability tokens.
    configure_logging(
        level=getattr(logging, config.log_level.upper()),
        json_format=config.log_json,
        file_path=config.log_file,
    )
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
