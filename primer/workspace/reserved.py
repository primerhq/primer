"""Which workspace paths are the runtime's own reserved trees.

``.state`` (the state repo: every session's messages.jsonl, session.json,
mounts.json) and ``.tmp`` (truncated tool outputs) live inside the
workspace root, so the raw file routes and tools could serve them like any
user file. The backends refuse WRITES there (``_refuse_reserved``) but must
keep reading them, because the runtime itself does; the user-facing raw
readers call :func:`reserved_tree` and refuse non-admins (A-22).
"""

from __future__ import annotations

import posixpath
from typing import Any

_DEFAULT_STATE_PATH = ".state"
_DEFAULT_TMP_PATH = ".tmp"


def _normalise(path: str) -> str:
    """Root-relative normal form: ``./x``, ``a/../x``, ``//x`` and ``/x``
    all become ``x``. A leading ``/`` is anchored before ``normpath`` so
    ``..`` can never climb above the root and survive as a prefix."""
    return posixpath.normpath("/" + path).lstrip("/")


def reserved_trees(workspace: Any) -> tuple[str, str]:
    """The workspace's configured ``(state_path, tmp_path)``."""
    template = getattr(workspace, "template", None)
    state = getattr(template, "state_path", None) or _DEFAULT_STATE_PATH
    tmp = getattr(template, "tmp_path", None) or _DEFAULT_TMP_PATH
    return state, tmp


def reserved_tree(path: str, trees: tuple[str, ...]) -> str | None:
    """The reserved tree ``path`` names or falls under, else ``None``."""
    norm = _normalise(path)
    for tree in trees:
        root = _normalise(tree)
        if norm == root or norm.startswith(root + "/"):
            return tree
    return None


__all__ = ["reserved_tree", "reserved_trees"]
