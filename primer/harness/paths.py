"""Paths that come out of a harness checkout are the repository author's, not the operator's: a symlink in the repo can name any file the server can read.

``symlink_reason`` is the one check every read of a harness-sourced file goes through (the template loader, the bundle-hash walks): a path
that is a symlink (or sits under one, up to the checkout root), or that resolves outside the root, is refused by the caller with a clear error naming
the entry.
"""

from __future__ import annotations

import os
from pathlib import Path


def symlink_reason(root: Path, path: Path) -> str | None:
    """Why ``path`` may not be read as part of the checkout at ``root``, or ``None`` when it is a plain entry inside it."""
    root_real = Path(os.path.realpath(root))
    current = path
    while current != root and current != current.parent:
        if current.is_symlink():
            return "is a symbolic link"
        current = current.parent
    real = Path(os.path.realpath(path))
    if real != root_real and root_real not in real.parents:
        return "resolves outside the harness checkout"
    return None


__all__ = ["symlink_reason"]
