"""Which workspace paths are the runtime's own reserved trees.

``.state`` (the state repo: every session's messages.jsonl, session.json,
mounts.json) and ``.tmp`` (truncated tool outputs) live inside the
workspace root, so the raw file routes and tools could serve them like any
user file. The backends refuse WRITES there but must keep reading them,
because the runtime itself does; the user-facing raw readers call
:func:`reserved_tree_for` and refuse non-admins (A-22).

A path is classified the way the backend RESOLVES it: each backend's
``reserved_tree_of`` runs its own resolver (the local backend follows
absolute paths and symlinks; the sandbox backend turns ``\\`` into ``/``
and anchors ``/x`` at the workspace root). :func:`reserved_tree` is the
string-only fallback for a workspace object without that method.
"""

from __future__ import annotations

import posixpath
from typing import Any

_DEFAULT_STATE_PATH = ".state"
_DEFAULT_TMP_PATH = ".tmp"


def _normalise(path: str) -> str:
    """Root-relative normal form: ``./x``, ``a/../x``, ``//x``, ``/x`` and
    backslash spellings all become ``x``. A leading ``/`` is anchored before
    ``normpath`` so ``..`` can never climb above the root and survive as a
    prefix."""
    return posixpath.normpath("/" + path.replace("\\", "/")).lstrip("/")


def reserved_trees(workspace: Any) -> tuple[str, str]:
    """The workspace's configured ``(state_path, tmp_path)``."""
    template = getattr(workspace, "template", None)
    state = getattr(template, "state_path", None) or _DEFAULT_STATE_PATH
    tmp = getattr(template, "tmp_path", None) or _DEFAULT_TMP_PATH
    return state, tmp


def reserved_tree(path: str, trees: tuple[str, ...]) -> str | None:
    """The reserved tree ``path`` names or falls under, by string, else
    ``None``. Used for backend-produced root-relative entry paths and as
    the fallback classifier."""
    norm = _normalise(path)
    for tree in trees:
        root = _normalise(tree)
        if norm == root or norm.startswith(root + "/"):
            return tree
    return None


def reserved_tree_for(workspace: Any, path: str) -> str | None:
    """The reserved tree ``path`` reaches when ``workspace`` resolves it.

    Delegates to the backend's ``reserved_tree_of`` (same resolver as its
    reads, so the guard and the read cannot disagree); raises what that
    resolver raises on an escape (``BadRequestError``). Falls back to the
    string classification for a workspace without the method.
    """
    classify = getattr(workspace, "reserved_tree_of", None)
    if callable(classify):
        return classify(path)
    return reserved_tree(path, reserved_trees(workspace))


__all__ = ["reserved_tree", "reserved_tree_for", "reserved_trees"]
