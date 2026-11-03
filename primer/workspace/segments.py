"""The segmented session log: segment names and the ``SessionLog`` reader facade (design D1 to D7, sections 3 and 5).

A session's log is ``concat(segment 0, 1, 2, ...)``: ``messages.jsonl`` is segment 0 (the legacy name, the whole log of a never-rotated
session), later segments are ``messages.0001.jsonl``, ``messages.0002.jsonl``, ... and the highest number is the active one. Every offset a
reader holds is an offset into that concatenation, so it stays valid as segments are added. Closed segments never change; their sizes are
cached per process.

Readers go through :class:`SessionLog` over a :class:`SegmentSource` (the five primitives a backend gives); three adapters bind it to a
workspace, a state repo and the ``read_lines`` test fakes.
"""

from __future__ import annotations

import json
import os
import re
import weakref
from collections.abc import Iterable
from typing import Any, NamedTuple, Protocol

from primer.model.except_ import NotFoundError

LOG_NAME = "messages.jsonl"
SEGMENT_RE = re.compile(r"messages\.(\d{4,})\.jsonl")
_MARKER = b"compaction_marker"


def segment_name(n: int) -> str:
    """``0`` is the legacy name; the others are zero padded to four digits (nothing may depend on the width)."""
    return LOG_NAME if n == 0 else f"messages.{n:04d}.jsonl"


def parse_segment(name: str) -> int | None:
    """The segment number of a log file name, or None for any other name (``messages.0000.jsonl`` is not segment 0)."""
    if name == LOG_NAME:
        return 0
    match = SEGMENT_RE.fullmatch(name)
    number = int(match.group(1)) if match else 0
    return number if number > 0 else None


def ordered(names: Iterable[str]) -> list[tuple[int, str]]:
    """The segment names among ``names``, ``(number, name)`` in numeric order."""
    found = {n: name for name in names if (n := parse_segment(name)) is not None}
    return sorted(found.items())


class SegmentGap(RuntimeError):
    """The segment numbers are not ``0..n`` contiguous: a middle file is missing, and the log cannot be read whole."""


class SegmentInfo(NamedTuple):
    index: int
    name: str
    size: int


class SegmentSource(Protocol):
    """What a backend gives the facade. A missing file answers ``None`` (``read``, ``size``) or raises ``NotFoundError`` as the backend does."""

    async def list_names(self, dir_rel: str) -> list[str]: ...

    async def size(self, rel: str) -> int | None: ...

    async def read(self, rel: str) -> bytes | None: ...

    async def read_from(self, rel: str, offset: int) -> bytes: ...

    async def read_tail(self, rel: str, max_bytes: int) -> tuple[bytes, bool]: ...


# ---- adapters ---------------------------------------------------------------------------------------------------------------------------------


class WorkspaceSource:
    """A workspace handle (paths relative to the workspace root): the reader routes, the relay, the tap and the usage fold.

    A minimal handle works too (a test fake with only ``read_file``): it is read as a single file, a size is the length of a read, and
    a read from an offset slices a whole read. The bytes of a size probe are kept for the read that follows, as in :class:`StateRepoSource`.
    """

    def __init__(self, workspace: Any) -> None:
        self.target = workspace
        self._probed: tuple[str, bytes] | None = None

    async def list_names(self, dir_rel: str) -> list[str]:
        self._probed = None
        listing = getattr(self.target, "list_files", None)
        if listing is None:
            # shortcut: a handle that cannot list is read as a single-file log (a test fake that serves one file for any path would be probed
            # forever); upgrade a fake that must serve several segments to a real listing.
            return [segment_name(0)] if await self.size(f"{dir_rel}/{segment_name(0)}") is not None else []
        try:
            entries = await listing(dir_rel)
        except NotFoundError:
            return []
        return [str(e.path).rsplit("/", 1)[-1] for e in entries]

    async def size(self, rel: str) -> int | None:
        info = getattr(self.target, "file_info", None)
        if info is not None:
            try:
                return (await info(rel)).size_bytes
            except NotFoundError:
                return None
        if self._probed is not None and self._probed[0] == rel:
            return len(self._probed[1])
        data = await self.read(rel)
        if data is not None:
            self._probed = (rel, data)
        return None if data is None else len(data)

    async def read(self, rel: str) -> bytes | None:
        if self._probed is not None and self._probed[0] == rel:
            data, self._probed = self._probed[1], None
            return data
        try:
            data = await self.target.read_file(rel)
        except NotFoundError:
            return None
        return data.encode("utf-8") if isinstance(data, str) else data    # a test fake may serve text

    async def read_from(self, rel: str, offset: int) -> bytes:
        ranged = getattr(self.target, "read_file_from", None)
        if ranged is None:
            return ((await self.read(rel)) or b"")[offset:]
        try:
            return await ranged(rel, offset)
        except NotFoundError:
            return b""

    async def read_tail(self, rel: str, max_bytes: int) -> tuple[bytes, bool]:
        from primer.workspace.tail_read import tail_of

        try:
            tail = await tail_of(self.target, rel, max_bytes)
            if tail is not None:
                return tail
        except NotFoundError:
            return b"", True
        data = (await self.read(rel)) or b""
        return data[-max_bytes:], len(data) <= max_bytes


class ShimSource:
    """The pool's IO shim (state-relative paths, a workspace id): the dispatch layer's turn-exit reads. It needs the shim's listing and ranged reads
    (``list_state_dir``, ``state_size``, ``read_state_from``); an object without them is a single-file test fake, which the caller keeps reading as before."""

    def __init__(self, shim: Any, workspace_id: str) -> None:
        self.target = shim
        self._wid = workspace_id

    @staticmethod
    def supports(shim: Any) -> bool:
        return all(callable(getattr(shim, name, None)) for name in ("list_state_dir", "state_size", "read_state_from", "read_state_tail"))

    async def list_names(self, dir_rel: str) -> list[str]:
        names = await self.target.list_state_dir(self._wid, dir_rel)
        if names is None:    # the workspace behind the shim cannot list: a single-file log
            return [segment_name(0)] if await self.size(f"{dir_rel}/{segment_name(0)}") is not None else []
        return names

    async def size(self, rel: str) -> int | None:
        return await self.target.state_size(self._wid, rel)

    async def read(self, rel: str) -> bytes | None:
        if await self.size(rel) is None:
            return None
        return await self.target.read_state_file(self._wid, rel)

    async def read_from(self, rel: str, offset: int) -> bytes:
        return await self.target.read_state_from(self._wid, rel, offset)

    async def read_tail(self, rel: str, max_bytes: int) -> tuple[bytes, bool]:
        try:
            tail = await self.target.read_state_tail(self._wid, rel, max_bytes)
        except NotFoundError:
            return b"", True
        if tail is not None:
            return tail
        data = (await self.read(rel)) or b""
        return data[-max_bytes:], len(data) <= max_bytes


async def read_active_segment(repo: Any, session_id: str) -> tuple[str, bytes]:
    """``(name, bytes)`` of the session log's ACTIVE segment as a state repo holds it: what a whole-file writer reads under the messages lock before
    it hands ``LogRewrite(name, bytes, new)`` to ``commit``. ``("messages.jsonl", b"")`` for a session with no log yet."""
    log = SessionLog(StateRepoSource(repo), f"sessions/{session_id}")
    active = await log.active()
    if active is None:
        return LOG_NAME, b""
    return active.name, (await repo.read_state_file(f"sessions/{session_id}/{active.name}")) or b""


def session_log_for(workspace: Any, session_id: str) -> "SessionLog":
    """The log of ``session_id`` read through a workspace handle (the routes, the relay, the tap): ``<state_path>/sessions/<id>``."""
    return SessionLog(WorkspaceSource(workspace), f"{getattr(workspace, 'state_path', '.state')}/sessions/{session_id}")


class StateRepoSource:
    """A state repo (paths relative to ``.state/``): the session and the executor. ``list_state_dir`` and ``stat_state_file`` are optional on a
    repo; without a listing the segments are probed in order, which is as good as a listing for a log whose numbering has no gap."""

    def __init__(self, repo: Any) -> None:
        self.target = repo
        # A repo that cannot stat tells a file's size by reading it. The bytes of that probe are kept for the read that follows, so a log
        # that must be read anyway is read ONCE per view, as it was before segments (shortcut: one file per source is held; a probe of
        # another replaces it, and a listing starts afresh).
        self._probed: tuple[str, bytes] | None = None

    async def list_names(self, dir_rel: str) -> list[str]:
        self._probed = None
        listing = getattr(self.target, "list_state_dir", None)
        if listing is not None:
            return list(await listing(dir_rel))
        names: list[str] = []
        n = 0
        while await self.size(f"{dir_rel}/{segment_name(n)}") is not None:
            names.append(segment_name(n))
            n += 1
        return names

    async def size(self, rel: str) -> int | None:
        stat = getattr(self.target, "stat_state_file", None)
        if stat is not None:
            found = await stat(rel)
            return None if found is None else found[0]
        if self._probed is not None and self._probed[0] == rel:
            return len(self._probed[1])
        data = await self.target.read_state_file(rel)
        if data is not None:
            self._probed = (rel, data)
        return None if data is None else len(data)

    async def read(self, rel: str) -> bytes | None:
        if self._probed is not None and self._probed[0] == rel:
            data, self._probed = self._probed[1], None
            return data
        return await self.target.read_state_file(rel)

    async def read_from(self, rel: str, offset: int) -> bytes:
        return ((await self.read(rel)) or b"")[offset:]

    async def read_tail(self, rel: str, max_bytes: int) -> tuple[bytes, bool]:
        data = (await self.read(rel)) or b""
        return data[-max_bytes:], len(data) <= max_bytes


class LinesSource:
    """A test fake that exposes ``read_lines(session_id, filename)`` (the dispatch tests' workspace IO); it cannot list, so segments are probed.

    shortcut: a blank line and an empty segment are invisible through ``read_lines``; upgrade a fake that needs them to a listing source.
    """

    def __init__(self, io: Any) -> None:
        self.target = io

    def _lines(self, rel: str) -> list[str] | None:
        session_id, filename = rel.rsplit("/", 2)[-2:]
        lines = self.target.read_lines(session_id, filename)
        return lines or None

    async def list_names(self, dir_rel: str) -> list[str]:
        names: list[str] = []
        while self._lines(f"{dir_rel}/{segment_name(len(names))}") is not None:
            names.append(segment_name(len(names)))
        return names

    async def read(self, rel: str) -> bytes | None:
        lines = self._lines(rel)
        return None if lines is None else ("\n".join(lines) + "\n").encode()

    async def size(self, rel: str) -> int | None:
        data = await self.read(rel)
        return None if data is None else len(data)

    async def read_from(self, rel: str, offset: int) -> bytes:
        return ((await self.read(rel)) or b"")[offset:]

    async def read_tail(self, rel: str, max_bytes: int) -> tuple[bytes, bool]:
        data = (await self.read(rel)) or b""
        return data[-max_bytes:], len(data) <= max_bytes


# ---- the facade -------------------------------------------------------------------------------------------------------------------------------

#: Closed segments never change, so their sizes are kept per process: ``{underlying backend object: {(slot, name): size}}``. Held weakly (a
#: workspace or repo that goes away takes its entries with it), and dropped for a name whose bytes disagree with it when they are read.
_SIZES: "weakref.WeakKeyDictionary[Any, dict[tuple[str, str], int]]" = weakref.WeakKeyDictionary()


def _size_cache(source: Any) -> dict[tuple[str, str], int]:
    target = getattr(source, "target", source)
    try:
        return _SIZES.setdefault(target, {})
    except TypeError:  # an object that cannot be weakly referenced: no cache, every size is asked
        return {}


class SessionLog:
    """One session's log, ``concat(segment 0, 1, ...)``, over ``source``; ``slot_rel`` is the session's directory in the source's own paths."""

    def __init__(self, source: SegmentSource, slot_rel: str) -> None:
        self._source = source
        self._slot = slot_rel.rstrip("/")
        self._sizes = _size_cache(source)

    def _rel(self, name: str) -> str:
        return f"{self._slot}/{name}"

    async def segments(self) -> list[SegmentInfo]:
        """The segments in order with their sizes. Every closed segment's size comes from the cache after its first stat; the active one is
        stat'd each time. Raises :class:`SegmentGap` when the numbering is not ``0..n``."""
        found = ordered(await self._source.list_names(self._slot))
        if [n for n, _ in found] != list(range(len(found))):
            raise SegmentGap(f"{self._slot}: segments {[n for n, _ in found]} are not 0..{len(found) - 1}")
        out: list[SegmentInfo] = []
        for position, (index, name) in enumerate(found):
            key = (self._slot, name)
            size = None if position == len(found) - 1 else self._sizes.get(key)
            if size is None:
                size = await self._source.size(self._rel(name))
                if size is None:
                    raise SegmentGap(f"{self._rel(name)} is listed but gone")
                if position < len(found) - 1:
                    self._sizes[key] = size
                else:
                    self._sizes.pop(key, None)
            out.append(SegmentInfo(index, name, size))
        return out

    async def active(self) -> SegmentInfo | None:
        segments = await self.segments()
        return segments[-1] if segments else None

    async def size(self) -> int:
        """The logical size: the bytes the single file would hold."""
        return sum(s.size for s in await self.segments())

    async def read_segment(self, info: SegmentInfo) -> bytes:
        """One segment's bytes, as :meth:`segments` listed it (for a reader that walks them newest first and stops early)."""
        return await self._read(info)

    async def _read(self, info: SegmentInfo) -> bytes:
        data = await self._source.read(self._rel(info.name))
        if data is None:
            raise SegmentGap(f"{self._rel(info.name)} is listed but gone")
        if len(data) != info.size:
            self._sizes.pop((self._slot, info.name), None)  # a closed segment that changed under the cache
        return data

    async def read_all(self) -> bytes:
        return b"".join([await self._read(s) for s in await self.segments()])

    async def read_from(self, offset: int, *, segments: list[SegmentInfo] | None = None) -> bytes:
        """The log's bytes from the logical ``offset`` (empty at or past the end). ``segments`` is a listing the caller just made."""
        out: list[bytes] = []
        start = 0
        for info in segments if segments is not None else await self.segments():
            end = start + info.size
            if end > offset:
                if offset <= start:
                    out.append(await self._read(info))
                else:
                    out.append(await self._source.read_from(self._rel(info.name), offset - start))
            start = end
        return b"".join(out)

    async def read_tail(self, max_bytes: int) -> tuple[bytes, bool]:
        """The last ``max_bytes`` of the log and whether that is the whole of it (only when byte 0 of segment 0 was reached)."""
        segments = await self.segments()
        total = sum(s.size for s in segments)
        if max_bytes >= total:
            return await self.read_all(), True
        parts: list[bytes] = []
        remaining = max_bytes
        for info in reversed(segments):
            if remaining <= 0:
                break
            if info.size <= remaining:
                parts.append(await self._read(info))
            else:
                parts.append((await self._source.read_tail(self._rel(info.name), remaining))[0])
            remaining -= info.size
        return b"".join(reversed(parts)), False

    async def since_last_marker(self, *, segments: list[SegmentInfo] | None = None) -> tuple[int, bytes]:
        """``(logical start offset, bytes)`` from the newest segment that holds a compaction marker on (the whole log without one).

        Everything before the newest marker is replaced by the marker's summary and kept tail, so a history reader needs no earlier segment. The
        test is a substring (a hint: a marker that was rewound away, or a message that quotes the word, only costs one wider read).
        """
        segments = segments if segments is not None else await self.segments()
        parts: list[bytes] = []
        first = 0
        for info in reversed(segments):
            data = await self._read(info)
            parts.append(data)
            first = info.index
            if _MARKER in data:
                break
        return sum(s.size for s in segments[:first]), b"".join(reversed(parts))

    async def max_event_seq(self) -> int:
        """The highest ``seq`` of any event record (a line with a ``kind`` and an integer ``seq``); message lines carry none, so a segment of
        bare messages is walked past. 0 when there is none."""
        for info in reversed(await self.segments()):
            best = 0
            for line in (await self._read(info)).split(b"\n"):
                if b'"seq"' not in line or b'"kind"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and "kind" in obj and isinstance(obj.get("seq"), int):
                    best = max(best, obj["seq"])
            if best:
                return best
        return 0


# ---- the configured segment size --------------------------------------------------------------------------------------------------------------

#: Rotate a log when its active segment holds this many bytes (the user's 1 MiB); the built-in default of a process that was never configured.
DEFAULT_SEGMENT_BYTES = 1_048_576
_segment_bytes_configured: int | None = None


def set_segment_bytes_default(n: int | None) -> None:
    """``WorkerConfig.session_log_segment_bytes``: rotate a session's log when its active segment holds this many bytes; 0 never rotates (the kill
    switch: existing segments are still read). ``None`` forgets the configuration."""
    global _segment_bytes_configured
    _segment_bytes_configured = None if n is None else max(0, int(n))


def segment_bytes_default() -> int:
    """An explicit setting (``set_segment_bytes_default``, or a configuration that is not the default) over ``PRIMER_SESSION_LOG_SEGMENT_BYTES`` (how a
    whole test suite is run segmented) over :data:`DEFAULT_SEGMENT_BYTES`."""
    if _segment_bytes_configured is not None:
        return _segment_bytes_configured
    env = os.environ.get("PRIMER_SESSION_LOG_SEGMENT_BYTES", "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return DEFAULT_SEGMENT_BYTES


__all__ = [
    "DEFAULT_SEGMENT_BYTES", "LOG_NAME", "LinesSource", "SEGMENT_RE", "SegmentGap", "SegmentInfo", "SegmentSource", "SessionLog", "ShimSource", "StateRepoSource", "WorkspaceSource",
    "ordered", "parse_segment", "read_active_segment", "segment_bytes_default", "segment_name", "session_log_for", "set_segment_bytes_default",
]
