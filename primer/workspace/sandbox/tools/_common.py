"""Path helpers shared by the sandbox tool set."""

from __future__ import annotations

from primer.model.except_ import BadRequestError


def resolve_sandbox_path(workspace_root: str, path: str) -> str:
    """Translate a workspace-relative path into a sandbox-absolute path.

    ``workspace_root`` is the sandbox-absolute path the agent sees as
    its CWD (typically ``/workspace``). The supplied ``path`` may be
    absolute (already includes ``workspace_root``) or relative. Rejects
    null bytes and ``..`` parent-escape attempts.
    """
    if not path:
        raise BadRequestError("path must be non-empty")
    if "\x00" in path:
        raise BadRequestError("path contains a null byte")

    root = workspace_root.rstrip("/") or "/"
    # If caller already supplied an absolute path that's inside the root,
    # honour it. If it's absolute and outside the root, reject.
    if path.startswith("/"):
        if path == root or path.startswith(root + "/"):
            normalised = _normalise_components(path)
            return normalised
        raise BadRequestError(
            f"path resolves outside workspace root {root!r}: {path!r}"
        )

    # Relative path: join under the root.
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise BadRequestError(
                    f"path resolves outside workspace: {path!r}"
                )
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        return root
    return f"{root}/{'/'.join(parts)}"


def workspace_relative(workspace_root: str, absolute: str) -> str:
    """Convert a sandbox-absolute path back to a relative form."""
    root = workspace_root.rstrip("/") or "/"
    if absolute == root:
        return "."
    prefix = root + "/"
    if absolute.startswith(prefix):
        return absolute[len(prefix):]
    return absolute


def _normalise_components(path: str) -> str:
    parts: list[str] = []
    for p in path.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if not parts:
                raise BadRequestError(
                    f"path resolves outside root: {path!r}"
                )
            parts.pop()
        else:
            parts.append(p)
    return "/" + "/".join(parts) if parts else "/"


__all__ = ["resolve_sandbox_path", "workspace_relative"]
