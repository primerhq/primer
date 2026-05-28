"""ContainerdRuntimeAdapter -- stub.

The full adapter would speak CRI v1 gRPC against
``/run/containerd/containerd.sock``. Phase B ships only the Docker
adapter; this module is a stub so callers fail at construct time with
a clear error rather than at import.
"""

from __future__ import annotations

from primer.model.except_ import ConfigError


class ContainerdRuntimeAdapter:
    """Stub. Raises :class:`ConfigError` on any operation."""

    def __init__(self, config) -> None:
        raise ConfigError(
            "ContainerdRuntimeAdapter is not yet implemented "
            "(Phase B follow-up)."
        )


__all__ = ["ContainerdRuntimeAdapter"]
