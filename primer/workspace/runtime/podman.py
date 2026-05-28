"""PodmanRuntimeAdapter -- stub.

Podman exposes a Docker-compatible REST API at its socket; the full
adapter would mirror :class:`DockerRuntimeAdapter` against that socket
via ``aiohttp``. Phase B ships only the Docker adapter; this module is
a stub so the factory can fail at construct time with a clear error
rather than ImportError on optional code paths.
"""

from __future__ import annotations

from primer.model.except_ import ConfigError


class PodmanRuntimeAdapter:
    """Stub. Raises :class:`ConfigError` on any operation."""

    def __init__(self, config) -> None:
        raise ConfigError(
            "PodmanRuntimeAdapter is not yet implemented (Phase B follow-up)."
        )


__all__ = ["PodmanRuntimeAdapter"]
