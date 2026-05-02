"""Helpers shared by every workspace tool.

Path resolution is the load-bearing concern: the agent supplies
relative paths in tool arguments, and we MUST keep them anchored
inside the workspace root regardless of ``..`` segments or absolute
prefixes. This is **advisory** -- the container / chroot is the real
sandbox boundary -- but it gives a clean :class:`BadRequestError` to
the LLM when it tries something obviously wrong.
"""

from __future__ import annotations

from pathlib import Path

from matrix.model.except_ import BadRequestError


def resolve_workspace_path(root: Path, rel: str) -> Path:
    """Resolve ``rel`` against ``root``; reject paths that escape.

    Returns the absolute path. The path doesn't have to exist; the
    caller decides what to do (read raises NotFoundError, write
    creates parents, etc.).
    """
    if not rel:
        raise BadRequestError("path must be non-empty")
    if "\x00" in rel:
        raise BadRequestError("path contains a null byte")
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise BadRequestError(
            f"path resolves outside workspace: {rel!r}"
        ) from exc
    return candidate


def workspace_relative(root: Path, absolute: Path) -> str:
    """Return ``absolute`` as a forward-slash path relative to ``root``."""
    return absolute.resolve().relative_to(root.resolve()).as_posix()


__all__ = [
    "resolve_workspace_path",
    "workspace_relative",
]
