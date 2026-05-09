"""Entry point: ``python -m matrix.api`` runs the API with uvicorn."""

from __future__ import annotations

import uvicorn

from matrix.api.app import create_app
from matrix.api.config import AppConfig


def main() -> None:  # pragma: no cover
    config = AppConfig()  # type: ignore[call-arg]
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
