"""Operator catalogue compatibility hints, never model execution authority.

The original proposal inspected upstream source at rust-v0.169.0. That is
not a live verification receipt, a minimum supported runtime, or evidence
that any named model is available to this account. No model-name allowlist
is inferred from it. A catalogue's explicit tool_mode is only a compatibility
hint; authenticated discovery and current policy must independently admit
the model before use.
"""

from __future__ import annotations

import json
import os
import stat

SOURCE_REFERENCE = "rust-v0.169.0"
MAX_CATALOG_BYTES = 4 * 1024 * 1024


def _catalog_tool_modes(catalog_path: str | None) -> dict[str, str | None]:
    """Read a bounded regular file through one nonblocking descriptor.

    Final-component symlinks are refused, including links swapped in during
    open. Ancestor directories follow normal host pathname resolution and
    must be operator-controlled; this reader is not a filesystem sandbox.
    Platforms without the required open flags refuse rather than fall back
    to a blocking or symlink-following open. O_NONBLOCK prevents a replaced
    FIFO from blocking open; it does not bound regular-file filesystem I/O.
    No pathname is reopened after fstat. Errors disclose no file contents.
    """
    if not catalog_path or not all(hasattr(os, flag) for flag in ("O_NONBLOCK", "O_NOFOLLOW", "O_CLOEXEC")):
        return {}
    fd = None
    close_failed = False
    try:
        fd = os.open(catalog_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CATALOG_BYTES:
            return {}
        raw = bytearray()
        while len(raw) <= MAX_CATALOG_BYTES:
            chunk = os.read(fd, min(65536, MAX_CATALOG_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_CATALOG_BYTES:
            return {}
    except (OSError, ValueError):
        return {}
    finally:
        if fd is not None:
            # Do not retry close: even a failed close may have released the
            # descriptor. An uncertain close must not admit a model.
            try:
                os.close(fd)
            except OSError:
                close_failed = True
    if close_failed:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}
    entries = data.get("models") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return {}
    out: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return {}
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug or slug in out:
            return {}
        tool_mode = entry.get("tool_mode")
        out[slug] = tool_mode if isinstance(tool_mode, str) else None
    return out


def is_direct_model(model: str, *, catalog_path: str | None) -> bool:
    """Whether the operator file explicitly describes direct tool syntax.

    This is not live evidence, account entitlement, or permission to execute.
    Missing, invalid, ambiguous or unreadable catalogues refuse every model,
    including names mentioned in the original source-derived proposal.
    """
    return _catalog_tool_modes(catalog_path).get(model) == "direct"
