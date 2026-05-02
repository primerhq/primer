"""Per-session output caching for the workspace's ``.tmp/`` directory.

Direct port of opencode's truncation pattern, extended with per-session
scoping. When a workspace tool produces output that exceeds the
configured size limits, the dispatch layer writes the full output to a
file under ``<workspace>/.tmp/<session_id>/`` and replaces the LLM-
visible result with a short preview plus a path the agent can read back
via the ``read`` tool.

Two exports:

* :class:`TruncationStore` -- workspace-scoped store with per-session
  subdirectories. One instance per :class:`matrix.int.Workspace`;
  :class:`matrix.workspace.session.AgentSession` is a thin wrapper that
  forwards its session id.
* :class:`TruncatedOutput` -- the model returned from
  :meth:`TruncationStore.output`.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` (the
"truncation cache" section) for the full design.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


# ===========================================================================
# Constants
# ===========================================================================


_DEFAULT_MAX_LINES = 2000
_DEFAULT_MAX_BYTES = 50 * 1024
_DEFAULT_RETENTION = timedelta(days=7)
_BACKGROUND_INTERVAL_SECONDS = 60 * 60  # one hour

# Filename shape: tool_<nanos>_<counter>.txt. Sortable, monotonic within a
# process, and decodable for retention sweeps.
_FILENAME_PATTERN = re.compile(r"^tool_(\d+)_(\d+)\.txt$")


# ===========================================================================
# Result model
# ===========================================================================


class TruncatedOutput(BaseModel):
    """The result of running text through :meth:`TruncationStore.output`."""

    content: str = Field(
        ...,
        description=(
            "What the LLM should see. Either the original text (when "
            "under the limits) or a preview followed by a hint pointing "
            "at the cached full output."
        ),
    )
    truncated: bool = Field(
        ...,
        description="True iff the original text was over the limits and was cached.",
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Absolute path to the cached full output when ``truncated`` "
            "is True. ``None`` when the original text fit under the limits."
        ),
    )


# ===========================================================================
# TruncationStore
# ===========================================================================


class TruncationStore:
    """Per-workspace store for oversized tool outputs.

    Owns the ``<workspace>/.tmp/`` directory. Each session writes into
    its own subdirectory ``<root>/<session_id>/`` so cleanup on session
    end is one ``rm -rf`` and listings stay tidy.

    Filenames within a session are ``tool_<nanos>_<counter>.txt`` where
    ``nanos`` is ``time.time_ns()`` at write time and ``counter`` is a
    monotonically-increasing per-process integer that disambiguates
    writes in the same nanosecond. The leading nanos is what the
    retention sweep reads to decide whether to delete a file -- there is
    no need to consult ``stat()``.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_lines: int = _DEFAULT_MAX_LINES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        retention: timedelta = _DEFAULT_RETENTION,
    ) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if retention.total_seconds() <= 0:
            raise ValueError("retention must be positive")

        self._root = Path(root)
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._retention = retention
        self._counter = 0
        self._counter_lock = asyncio.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    # ---- public surface --------------------------------------------------

    @property
    def root(self) -> Path:
        """The ``.tmp/`` directory this store manages."""
        return self._root

    async def output(
        self,
        text: str,
        *,
        session_id: str,
        max_lines: int | None = None,
        max_bytes: int | None = None,
        direction: Literal["head", "tail"] = "head",
    ) -> TruncatedOutput:
        """Apply the truncation policy to ``text``.

        Returns ``text`` unchanged (with ``truncated=False``) when the
        text is under both the line and byte limits. Otherwise writes
        the full text to ``<root>/<session_id>/tool_<id>.txt``, builds a
        head- or tail-anchored preview, and returns the preview plus the
        cache path.

        ``max_lines`` and ``max_bytes`` override this store's defaults
        for one call (used by tools whose appropriate limit differs from
        the workspace default).
        """
        _validate_session_id(session_id)
        line_limit = max_lines if max_lines is not None else self._max_lines
        byte_limit = max_bytes if max_bytes is not None else self._max_bytes
        if line_limit < 1:
            raise ValueError("max_lines must be >= 1")
        if byte_limit < 1:
            raise ValueError("max_bytes must be >= 1")

        encoded = text.encode("utf-8")
        line_count = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
        if len(encoded) <= byte_limit and line_count <= line_limit:
            return TruncatedOutput(content=text, truncated=False, output_path=None)

        path = await self.write(text, session_id=session_id)
        preview = _build_preview(
            text,
            line_limit=line_limit,
            byte_limit=byte_limit,
            direction=direction,
            path=path,
        )
        return TruncatedOutput(
            content=preview,
            truncated=True,
            output_path=str(path),
        )

    async def write(self, text: str, *, session_id: str) -> Path:
        """Write ``text`` to a fresh file in this session's subdirectory.

        Always writes regardless of the configured size limits. The
        caller -- typically :meth:`output` -- decides the truncation
        policy; this method is the unconditional store. Returns the
        absolute path of the file just written.
        """
        _validate_session_id(session_id)
        session_dir = self._root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        filename = await self._next_filename()
        path = session_dir / filename
        await asyncio.to_thread(path.write_text, text, encoding="utf-8")
        logger.debug(
            "TruncationStore wrote cache file",
            extra={
                "session_id": session_id,
                "path": str(path),
                "size_bytes": len(text.encode("utf-8")),
            },
        )
        return path

    async def cleanup(self) -> int:
        """Walk every session subdirectory; delete files past retention.

        Returns the total number of files removed. Empty session
        subdirectories are NOT removed -- a session that has writes
        coming again would just have to re-create the directory.
        """
        cutoff_nanos = time.time_ns() - int(self._retention.total_seconds() * 1_000_000_000)
        removed = 0
        if not self._root.exists():
            return 0
        for session_dir in await asyncio.to_thread(_list_subdirs, self._root):
            for entry in await asyncio.to_thread(_list_files, session_dir):
                match = _FILENAME_PATTERN.match(entry.name)
                if match is None:
                    continue
                file_nanos = int(match.group(1))
                if file_nanos < cutoff_nanos:
                    try:
                        await asyncio.to_thread(entry.unlink)
                        removed += 1
                    except FileNotFoundError:
                        # Concurrent cleanup beat us to it; fine.
                        pass
        if removed:
            logger.info(
                "TruncationStore retention sweep removed cache files",
                extra={"removed": removed, "root": str(self._root)},
            )
        return removed

    async def cleanup_session(self, session_id: str) -> int:
        """Remove this session's entire cache subdirectory.

        Called when a session moves to ENDED so its disk footprint is
        released immediately rather than waiting for the retention sweep.
        Returns the number of cache files removed (0 if the directory
        didn't exist).
        """
        _validate_session_id(session_id)
        session_dir = self._root / session_id
        if not session_dir.exists():
            return 0
        files = await asyncio.to_thread(_list_files, session_dir)
        count = sum(1 for f in files if _FILENAME_PATTERN.match(f.name) is not None)
        await asyncio.to_thread(shutil.rmtree, session_dir)
        logger.info(
            "TruncationStore reaped session cache directory",
            extra={"session_id": session_id, "removed": count},
        )
        return count

    def start_background_cleanup(
        self,
        *,
        interval_seconds: float = _BACKGROUND_INTERVAL_SECONDS,
    ) -> asyncio.Task[None]:
        """Schedule periodic :meth:`cleanup`.

        Returns an :class:`asyncio.Task` the caller is responsible for
        cancelling on workspace shutdown. The task swallows individual
        cleanup errors (logging at WARNING) so a single sweep failure
        doesn't kill the loop.
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        return asyncio.create_task(self._background_loop(interval_seconds))

    # ---- internals -------------------------------------------------------

    async def _next_filename(self) -> str:
        """Return a fresh ``tool_<nanos>_<counter>.txt`` name."""
        async with self._counter_lock:
            self._counter += 1
            counter = self._counter
        nanos = time.time_ns()
        return f"tool_{nanos}_{counter:08d}.txt"

    async def _background_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.cleanup()
            except asyncio.CancelledError:
                # Propagate cancellation so the caller can detect shutdown.
                raise
            except Exception as exc:  # noqa: BLE001 -- never let the loop die
                logger.warning(
                    "TruncationStore background cleanup failed",
                    extra={"error": str(exc), "root": str(self._root)},
                )


# ===========================================================================
# Helpers
# ===========================================================================


def _validate_session_id(session_id: str) -> None:
    """Reject session ids that would let writes escape ``<root>/<session_id>/``."""
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"session_id contains illegal characters: {session_id!r}")
    if "\x00" in session_id:
        raise ValueError("session_id contains a null byte")


def _build_preview(
    text: str,
    *,
    line_limit: int,
    byte_limit: int,
    direction: Literal["head", "tail"],
    path: Path,
) -> str:
    """Build the LLM-visible preview string.

    Honours both the line cap and the byte cap; whichever is hit first
    bounds the preview. ``direction`` selects head- vs tail-anchored
    extraction.
    """
    lines = text.splitlines(keepends=True)
    if direction == "head":
        slice_lines = lines[:line_limit]
    else:
        slice_lines = lines[-line_limit:] if line_limit < len(lines) else lines
    preview = "".join(slice_lines)
    encoded = preview.encode("utf-8")
    if len(encoded) > byte_limit:
        if direction == "head":
            preview = encoded[:byte_limit].decode("utf-8", errors="ignore")
        else:
            preview = encoded[-byte_limit:].decode("utf-8", errors="ignore")
    hint = (
        f"\n\nThe tool call succeeded but the output was truncated.\n"
        f"Full output saved to: {path}\n"
        f"Use grep to search the full content or read with offset/limit "
        f"to view specific sections."
    )
    return preview + hint


def _list_subdirs(root: Path) -> list[Path]:
    return [entry for entry in root.iterdir() if entry.is_dir()]


def _list_files(directory: Path) -> list[Path]:
    return [entry for entry in directory.iterdir() if entry.is_file()]


__all__ = [
    "TruncatedOutput",
    "TruncationStore",
]
