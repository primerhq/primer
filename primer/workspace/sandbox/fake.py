"""FakeSandbox -- in-process :class:`Sandbox` implementation for tests + dev.

Backs ops with a host ``tempfile.TemporaryDirectory`` (caller-supplied).
Maps every ``/workspace/...`` path to ``<root>/...``. ``exec`` runs via
``asyncio.create_subprocess_shell`` with ``cwd=<root>/<workdir-stripped>``.

This file is production code (importable from non-test code) but is
intended only for tests and single-process dev. It is excluded from the
coverage gate the same way ``InMemoryScheduler`` is.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import stat as _stat_mod
import tarfile
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from primer.int.sandbox import (
    ExecResult,
    FileStat,
    Sandbox,
    SandboxInspectInfo,
)
from primer.workspace._locks import WorkspaceLockTable
from primer.workspace.runtime.protocol import ErrorCode
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError


_TAR_CHUNK_BYTES = 64 * 1024
_WORKSPACE_PREFIX = "/workspace"
# Mirrors the runtime's per-line match bound (primer_runtime.ops._SEARCH_LINE_TIMEOUT_S)
# and its default output cap (the grep tool sends both explicitly).
_SEARCH_LINE_TIMEOUT_S = 2.0
_SEARCH_MAX_OUTPUT_BYTES_DEFAULT = 4 << 20
#: Binary sniff, the same one the runtime's _read_capped (primer_runtime.ops) and the
#: local/sandbox read+grep tools apply: a NUL in the first 1024 bytes is a binary file,
#: which a search skips rather than matching.
_BINARY_SNIFF_BYTES = 1024
#: Per-file read codes that mean "this entry is unreadable" rather than "the runtime
#: failed" (a file that vanished, a permission, a symlinked directory listed as a
#: file): the runtime's search op skips such a file - its per-file read failed the same
#: way its os.open does - and only an op-level failure propagates. The legacy grep
#: walk's read classification is the same set (sandbox/tools/grep.py _UNREADABLE).
_UNREADABLE = frozenset({ErrorCode.ENOENT, ErrorCode.EACCES, ErrorCode.EISDIR, ErrorCode.ENOTDIR})


def _strip_leading_slash(path: str) -> str:
    """Convert ``/foo/bar`` to ``foo/bar``; leave relative paths alone."""
    return path.lstrip("/")


def _find_posix_shell() -> str | None:
    """Find a POSIX-style shell for running shell-string commands.

    Production sandboxes run Linux shells inside containers. FakeSandbox
    is a test seam; on Windows we look for git-for-windows' bash so
    POSIX shell scripts in tests behave the same as in real sandboxes.
    Returns the shell path or ``None`` if no POSIX shell is found
    (caller falls back to the OS default).
    """
    if os.name != "nt":
        # POSIX systems: just use ``sh`` via shell=True semantics.
        return None
    # Windows: prefer bash from git-for-windows.
    for candidate in ("bash", "bash.exe"):
        path = shutil.which(candidate)
        if path is not None:
            return path
    for candidate in ("sh.exe", "sh"):
        path = shutil.which(candidate)
        if path is not None:
            return path
    return None


_POSIX_SHELL = _find_posix_shell()


def _parse_trailers(message: str) -> dict[str, str]:
    """Extract ``Key: Value`` trailer lines from a git commit message.

    Used by :meth:`FakeSandbox.state_history` to surface the
    ``X-Primer-*`` trailers (workspace / session / agent / op) the state
    repo embeds in every commit, matching the flat shape the real runtime
    returns. Only lines of the form ``Key: Value`` are recognised.
    """
    trailers: dict[str, str] = {}
    for line in message.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key and value:
            trailers[key] = value
    return trailers


class FakeSandbox(Sandbox):
    """In-process Sandbox impl backed by a host tempdir.

    Used by every test that wants to exercise sandbox-tool / state /
    cache code without standing up a real container. Production code
    should not depend on this; it's exposed only because tests dispatch
    real workspace tools through it.

    Satisfies the :class:`~primer.workspace.sandbox.state._StateCapableSandbox`
    structural protocol so that :meth:`SandboxStateRepo.initialize` skips
    the exec-based git init (which would fail on host paths). The state ops
    are in-memory stubs that return plausible no-op results.
    """

    #: Protocol version advertised to SandboxStateRepo.  Set >= "1.1" so
    #: _require_state_ops() accepts this sandbox in tests that exercise the
    #: full state-op surface.
    protocol_version: str = "1.1"

    def __init__(self, root: Path, *, sandbox_id: str = "fake") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._id = sandbox_id
        self._stopped = False
        self._started_at = datetime.now(tz=timezone.utc)
        # Mirrors the runtime's advisory write-lock table so unit tests can
        # observe same-workdir write-exec serialization without a container.
        self._locks = WorkspaceLockTable()
        # In-memory state store: path -> bytes.  Used by the stub state ops.
        self._state_files: dict[str, bytes] = {}
        self._state_commits: list[dict] = []

    @property
    def id(self) -> str:
        return self._id

    def _host_path(self, sandbox_path: str) -> Path:
        """Map ``/workspace/foo`` and friends to ``<root>/foo``."""
        rel = _strip_leading_slash(sandbox_path)
        # Strip the leading "workspace/" so the agent's view of /workspace
        # maps onto the tempdir root.
        if rel == "workspace":
            return self._root
        if rel.startswith("workspace/"):
            rel = rel[len("workspace/"):]
        if not rel:
            return self._root
        return (self._root / rel)

    async def exec(
        self,
        command,
        *,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        stdin: bytes | None = None,
        abort: asyncio.Event | None = None,
        access: str = "write",
        writes: list[str] | None = None,
    ) -> ExecResult:
        cwd = self._host_path(workdir)
        if access == "read":
            lock_ctx = nullcontext()
        elif writes:
            lock_ctx = self._locks.hold_paths(
                [str(self._host_path(w)) for w in writes]
            )
        else:
            lock_ctx = self._locks.hold_scope(str(cwd))
        async with lock_ctx:
            return await self._exec_unlocked(
                command,
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
                stdin=stdin,
                abort=abort,
            )

    async def _exec_unlocked(
        self,
        command,
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: float | None,
        stdin: bytes | None,
        abort: asyncio.Event | None,
    ) -> ExecResult:
        if self._stopped:
            raise RuntimeError("sandbox is stopped")
        await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
        start = time.perf_counter()

        if isinstance(command, list):
            proc = await asyncio.create_subprocess_exec(
                *command, cwd=str(cwd), env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        elif _POSIX_SHELL is not None:
            # Force a POSIX shell so test scripts written for real
            # (Linux-container) sandboxes behave the same way locally.
            proc = await asyncio.create_subprocess_exec(
                _POSIX_SHELL, "-c", command, cwd=str(cwd), env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=str(cwd), env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        async def _abort_waiter() -> None:
            if abort is None:
                return
            await abort.wait()
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        abort_task = asyncio.create_task(_abort_waiter()) if abort else None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=timeout_seconds,
            )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
        finally:
            if abort_task is not None:
                abort_task.cancel()

        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_seconds=time.perf_counter() - start,
        )

    async def read_file(self, path: str) -> bytes:
        target = self._host_path(path)
        return await asyncio.to_thread(target.read_bytes)

    async def read_file_range(self, path: str, offset: int, limit: int) -> bytes:
        """The ``[offset, offset + limit)`` byte window of *path* (host FS).

        The sandbox ``read`` tool's bounded window read (review 2026-11-23,
        5.12) runs through this in tests, as it runs through the runtime's
        ``read_file(limit)`` in production. A missing file raises
        ``FileNotFoundError`` (the ENOENT of the host filesystem), like
        :meth:`read_file`.
        """
        target = self._host_path(path)

        def _read() -> bytes:
            with target.open("rb") as fh:
                if offset:
                    fh.seek(offset)
                return fh.read(limit)

        return await asyncio.to_thread(_read)

    async def walk(
        self,
        path: str,
        *,
        max_entries: int = 25000,
        timeout_s: float = 30.0,
    ) -> tuple[list[dict], bool]:
        """One recursive, capped walk of *path* (host FS).

        Same shape as the runtime's ``walk`` op (protocol 1.8): entries with
        their path RELATIVE to *path*, ``is_dir``, ``size`` (0 for
        directories) and ``mtime``; directories and files both count against
        *max_entries*. Symlinks are reported (a symlinked directory is a
        non-directory entry) but never followed. A directory that vanishes
        mid-walk is skipped, and a ``timeout_s`` deadline stops the whole
        walk (``truncated``), as in the runtime. The all-entries walk has no
        ``glob`` filter (the glob-aware files-only walk is the :meth:`search`
        op's), so nothing here mirrors the runtime's glob-before-count order.
        """
        root = self._host_path(path)
        if not await asyncio.to_thread(root.is_dir):
            raise FileNotFoundError(f"not a directory: {path!r}")

        def _do_walk() -> tuple[list[dict], bool]:
            # Breadth-first, each directory's children name-sorted, and a cap stops the WHOLE
            # walk: the same order and caps as the runtime's walk op.
            entries: list[dict] = []
            truncated = False
            deadline = time.monotonic() + timeout_s
            queue: deque[tuple[Path, str]] = deque([(root, "")])
            while queue and not truncated:
                directory, rel_prefix = queue.popleft()
                if time.monotonic() > deadline:
                    truncated = True
                    break
                try:
                    children = sorted(directory.iterdir(), key=lambda p: p.name)
                except OSError:
                    continue  # raced with a delete, as the runtime's walk skips it
                for child in children:
                    if time.monotonic() > deadline:
                        truncated = True
                        break
                    rel = f"{rel_prefix}/{child.name}" if rel_prefix else child.name
                    try:
                        st = child.lstat()  # a symlinked dir is a non-directory entry, as the runtime's walk does
                    except OSError:
                        continue
                    is_dir = _stat_mod.S_ISDIR(st.st_mode)
                    if is_dir:
                        queue.append((child, rel))
                    entries.append(
                        {
                            "path": rel,
                            "is_dir": is_dir,
                            "size": 0 if is_dir else int(st.st_size),
                            "mtime": float(st.st_mtime),
                        }
                    )
                    if len(entries) >= max_entries:
                        truncated = True
                        break
            return entries, truncated

        return await asyncio.to_thread(_do_walk)

    async def search(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        output_mode: str = "files_with_matches",
        context: int = 0,
        multiline: bool = False,
        case_insensitive: bool = False,
        skip: tuple[str, ...] = (),
        max_files: int = 2000,
        max_file_bytes: int = 1 << 20,
        max_matches: int = 5000,
        head_limit: int | None = None,
        max_output_bytes: int | None = None,
        timeout_s: float = 30.0,
    ) -> dict:
        """One recursive regex search of *path* (host FS).

        The FakeSandbox twin of the runtime's ``search`` op (protocol 1.8):
        the same breadth-first, name-sorted, files-only walk with the
        ``glob`` filter applied BEFORE the file cap counts, the same caps
        and stop reasons (``size_cap``, ``file_cap`` - flagged only when a
        file was actually dropped -, ``match_cap``, ``time_cap``,
        ``regex_timeout``, ``output_cap``; ``head_limit`` cuts the lines
        only), the same line shapes (``files_with_matches`` is one path per
        file, ``count`` is ``path:hits``, ``content`` is ``path:lineno:line``
        with ``context`` merged, multiline ``content`` is ``path::snippet``
        with newlines escaped) and the same
        ``{lines, match_count, file_count, truncated, stop_reason,
        stop_reasons, skipped_large}`` answer, with paths RELATIVE TO THE
        WORKSPACE ROOT. The pattern is matched with the ``regex`` library
        under a per-line timeout with the GIL released, exactly like the
        runtime, so the 1.8 grep tool path is tested end to end.
        """
        import regex

        from primer.workspace.local.tools.grep import glob_matches

        root = self._host_path(path)
        exists = await asyncio.to_thread(root.is_dir) or await asyncio.to_thread(root.is_file)
        if not exists:
            raise FileNotFoundError(f"no such file or directory: {path!r}")

        flags = 0
        if case_insensitive:
            flags |= regex.IGNORECASE
        if multiline:
            flags |= regex.DOTALL | regex.MULTILINE
        try:
            rx = regex.compile(pattern, flags)
        except (regex.error, OverflowError, RecursionError, MemoryError) as exc:
            raise ValueError(f"pattern does not compile: {exc}") from None

        if max_output_bytes is None:
            max_output_bytes = _SEARCH_MAX_OUTPUT_BYTES_DEFAULT

        # The workspace-relative path of the search root, as the runtime
        # computes it: output paths and the reserved-tree skip are both
        # relative to the WORKSPACE, not to *path*.
        try:
            base_rel = root.relative_to(self._root).as_posix()
        except ValueError:
            base_rel = ""
        if base_rel == ".":
            base_rel = ""

        deadline = time.monotonic() + timeout_s
        reasons: list[str] = []

        def note(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        def _do_walk() -> list[str]:
            candidates: list[str]
            if root.is_dir():
                # Breadth-first files-only walk, name-sorted children, the
                # reserved trees skipped, the glob filter BEFORE the file
                # cap counts: the runtime's _walk_tree(files_only=True,
                # file_filter=...) order, host-side.
                candidates = []
                cap_pending = False
                queue: deque[tuple[Path, str]] = deque([(root, "")])
                while queue and not cap_pending and time.monotonic() <= deadline:
                    directory, rel_prefix = queue.popleft()
                    try:
                        children = sorted(directory.iterdir(), key=lambda p: p.name)
                    except OSError:
                        continue  # raced with a delete, as the runtime's walk skips it
                    for index, child in enumerate(children):
                        if time.monotonic() > deadline:
                            note("time_cap")
                            queue.clear()
                            break
                        rel = f"{rel_prefix}/{child.name}" if rel_prefix else child.name
                        if skip:
                            ws_rel = f"{base_rel}/{rel}" if base_rel else rel
                            if any(ws_rel == s or ws_rel.startswith(s + "/") for s in skip):
                                continue
                        try:
                            st = child.lstat()
                        except OSError:
                            continue
                        if _stat_mod.S_ISDIR(st.st_mode):
                            queue.append((child, rel))
                            continue
                        if not _stat_mod.S_ISREG(st.st_mode):
                            continue  # a symlinked file or FIFO: the runtime's walk skips it
                        if glob is not None and not glob_matches(rel, glob):
                            continue  # the filter runs before the cap counts
                        candidates.append(rel)
                        if len(candidates) >= max_files:
                            cap_pending = (
                                index + 1 < len(children) or bool(queue)
                            )
                            break
                if cap_pending:
                    # The cap left files unvisited: one was actually dropped.
                    # A tree of exactly max_files is not cut, so no flag.
                    note("file_cap")
            else:
                # A file target searches the file itself (the platform sends
                # directories, but a direct file stays a one-file search),
                # as the runtime does.
                if glob is not None and not glob_matches(root.name, glob):
                    candidates = []
                else:
                    candidates = [""]
            return candidates

        candidates = await asyncio.to_thread(_do_walk)

        async def _do_match() -> dict:
            # The file reads run on the loop, one await per file through the
            # sandbox read hook - the seam where a subclass simulates the
            # runtime's per-file read failures (it can make a listed entry
            # unreadable); the only host-FS sweep is the walk, off the loop.
            out_lines: list[str] = []
            out_bytes = 0
            lines_cut = False
            match_count = 0
            file_count = 0
            skipped_large = 0
            halt = False

            def emit(line: str) -> None:
                nonlocal out_bytes, lines_cut
                if lines_cut:
                    return
                if head_limit is not None and len(out_lines) >= head_limit:
                    lines_cut = True
                    return
                size = len(line.encode("utf-8", errors="replace")) + 1
                if out_bytes + size > max_output_bytes:
                    lines_cut = True
                    note("output_cap")
                    return
                out_lines.append(line)
                out_bytes += size

            def one_search_timeout() -> float:
                return min(_SEARCH_LINE_TIMEOUT_S, max(deadline - time.monotonic(), 0.001))

            for rel in candidates:
                if halt:
                    break
                if time.monotonic() > deadline:
                    note("time_cap")
                    break
                rel_out = f"{base_rel}/{rel}" if base_rel and rel else (rel or base_rel)
                host_path = root if rel == "" else root / rel
                try:
                    fd = os.open(
                        host_path,
                        os.O_RDONLY
                        | os.O_NOFOLLOW
                        | os.O_NONBLOCK
                        | getattr(os, "O_CLOEXEC", 0),
                    )
                except OSError:
                    continue  # a symlink swapped in, or a file gone: skip, as the runtime does
                try:
                    st = os.fstat(fd)
                    if not _stat_mod.S_ISREG(st.st_mode):
                        continue  # a FIFO or device swapped in after the walk: never read it
                    if st.st_size > max_file_bytes:
                        skipped_large += 1
                        note("size_cap")
                        continue  # skip this file, keep searching the rest
                finally:
                    os.close(fd)
                # The body comes through the sandbox read hook, not a raw host read:
                # it is the seam where a subclass simulates the runtime's per-file
                # read failures (a symlinked directory listed as a file, a file that
                # vanished), which the runtime's search op skips the same way its
                # os.open does - and a runtime failure (any other code) raises, as
                # the real runtime's op-level error response would.
                sandbox_path = path if rel == "" else f"{path}/{rel}"
                try:
                    body = await self.read_file(sandbox_path)
                except OSError:
                    continue  # gone between the walk and the read: skip, as the runtime does
                except RuntimeOpError as exc:
                    if exc.code not in _UNREADABLE:
                        raise  # a runtime failure: the search op never swallows it
                    continue
                if b"\x00" in body[:_BINARY_SNIFF_BYTES]:
                    continue  # a binary file: the runtime's _read_capped answers None and the op skips it
                text = body.decode("utf-8", errors="replace")

                if multiline:
                    matches: list = []
                    try:
                        for m in rx.finditer(text, timeout=one_search_timeout(), concurrent=True):
                            matches.append(m)
                            if match_count + len(matches) >= max_matches:
                                break
                    except TimeoutError:
                        note("time_cap" if time.monotonic() > deadline else "regex_timeout")
                        halt = True
                    if match_count + len(matches) >= max_matches:
                        note("match_cap")
                        halt = True
                    if not matches:
                        continue
                    if output_mode == "files_with_matches":
                        emit(rel_out)
                    elif output_mode == "count":
                        emit(f"{rel_out}:{len(matches)}")
                    else:
                        for m in matches:
                            emit(f"{rel_out}::{m.group(0).replace(chr(10), chr(92) + 'n')}")
                    match_count += len(matches)
                    file_count += 1
                    continue

                all_lines = text.splitlines()
                hits: list[int] = []
                for index, line in enumerate(all_lines):
                    if time.monotonic() > deadline:
                        note("time_cap")
                        halt = True
                        break
                    try:
                        found = (
                            rx.search(line, timeout=one_search_timeout(), concurrent=True)
                            is not None
                        )
                    except TimeoutError:
                        note("time_cap" if time.monotonic() > deadline else "regex_timeout")
                        halt = True
                        break
                    if found:
                        hits.append(index)
                        if match_count + len(hits) >= max_matches:
                            note("match_cap")
                            halt = True
                            break
                if not hits:
                    continue
                match_count += len(hits)
                file_count += 1
                if output_mode == "files_with_matches":
                    emit(rel_out)
                elif output_mode == "count":
                    emit(f"{rel_out}:{len(hits)}")
                else:
                    emitted: set[int] = set()
                    for index in hits:
                        start = max(0, index - context)
                        end = min(len(all_lines), index + 1 + context)
                        for i in range(start, end):
                            if i in emitted:
                                continue
                            emitted.add(i)
                            emit(f"{rel_out}:{i + 1}:{all_lines[i]}")

            return {
                "lines": out_lines,
                "match_count": match_count,
                "file_count": file_count,
                "truncated": bool(reasons) or lines_cut,
                "stop_reason": reasons[0] if reasons else None,
                "stop_reasons": reasons,
                "skipped_large": skipped_large,
            }

        return await _do_match()

    async def write_file_cas(
        self,
        path: str,
        expected: bytes,
        content: bytes,
        *,
        mode: int | None = None,
    ) -> bool:
        """Compare-and-swap write (host FS), the FakeSandbox twin of the
        runtime's ``write_file_cas`` op (protocol 1.8).

        Under the same per-path write lock as :meth:`write_file`, the
        current bytes are read and *content* written only if they equal
        *expected*; a mismatch is ``False`` (the file is left untouched), a
        missing file raises ``FileNotFoundError``.
        """
        target = self._host_path(path)

        def _cas() -> bool:
            current = target.read_bytes()
            if current != expected:
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if mode is not None:
                try:
                    target.chmod(mode)
                except (OSError, NotImplementedError):
                    pass
            return True

        async with self._locks.hold_write(str(target.parent), str(target)):
            return await asyncio.to_thread(_cas)

    async def write_file(
        self, path: str, content: bytes, *, mode: int | None = None,
    ) -> None:
        target = self._host_path(path)
        async with self._locks.hold_write(str(target.parent), str(target)):
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, content)
            if mode is not None:
                try:
                    await asyncio.to_thread(target.chmod, mode)
                except (OSError, NotImplementedError):
                    pass

    async def append_file(self, path: str, content: bytes) -> None:
        """Atomically append ``content`` to ``path`` (fast O_APPEND path)."""
        target = self._host_path(path)

        def _append() -> None:
            with target.open("ab") as fh:
                fh.write(content)

        async with self._locks.hold_write(str(target.parent), str(target)):
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_append)

    async def make_dir(self, path: str) -> None:
        target = self._host_path(path)
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)

    async def list_dir(self, path: str) -> list[FileStat]:
        host = self._host_path(path)
        if not await asyncio.to_thread(host.is_dir):
            return []
        entries = await asyncio.to_thread(
            lambda: sorted(host.iterdir(), key=lambda p: p.name)
        )
        return [self._stat_from_path(entry, host) for entry in entries]

    async def stat(self, path: str) -> FileStat | None:
        host = self._host_path(path)
        if not await asyncio.to_thread(host.exists):
            return None
        info = self._stat_from_path(host, self._root)
        # The runtime's ``stat`` answers the RESOLVED path (it follows symlinks): the platform classifies reserved trees on it (W55d).
        try:
            resolved = host.resolve().relative_to(self._root.resolve()).as_posix() or "."
        except ValueError:
            resolved = host.resolve().name
        return info.model_copy(update={"path": resolved})

    def _stat_from_path(self, host: Path, anchor: Path) -> FileStat:
        st = host.stat()
        if host.is_symlink():
            kind = "symlink"
        elif host.is_dir():
            kind = "dir"
        else:
            kind = "file"
        try:
            rel = host.relative_to(anchor).as_posix()
        except ValueError:
            rel = host.name
        return FileStat(
            path=rel,
            kind=kind,  # type: ignore[arg-type]
            size_bytes=st.st_size if kind == "file" else 0,
            mode=st.st_mode,
            modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        )

    async def delete(self, path: str) -> None:
        target = self._host_path(path)
        if not await asyncio.to_thread(target.exists):
            return
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.rmtree, target)
        else:
            await asyncio.to_thread(target.unlink)

    async def archive(self, paths: list[str]) -> AsyncIterator[bytes]:
        members = [self._host_path(p) for p in paths]
        buf = io.BytesIO()

        def _build() -> None:
            with tarfile.open(fileobj=buf, mode="w") as tf:
                for m in members:
                    if not m.exists():
                        continue
                    tf.add(str(m), arcname=m.name, recursive=True)

        await asyncio.to_thread(_build)
        buf.seek(0)
        while True:
            chunk = buf.read(_TAR_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    async def inspect(self) -> SandboxInspectInfo:
        return SandboxInspectInfo(
            state="stopped" if self._stopped else "running",
            started_at=self._started_at,
        )

    async def stop(self) -> None:
        self._stopped = True

    async def remove(self) -> None:
        self._stopped = True
        await asyncio.to_thread(shutil.rmtree, self._root, ignore_errors=True)

    # ------------------------------------------------------------------
    # _StateCapableSandbox stub ops
    # ------------------------------------------------------------------

    async def state_commit(
        self,
        *,
        files: dict[str, bytes],
        deletes: list[str],
        message: str,
        allow_empty: bool = False,
    ) -> str:
        """In-memory state commit stub.

        Stores files in ``_state_files``, removes deleted paths, and
        records a commit entry.  Returns a deterministic fake SHA derived
        from the current commit count.
        """
        for path, content in files.items():
            self._state_files[path] = content
        for path in deletes:
            self._state_files.pop(path, None)
        sha = f"{len(self._state_commits):040x}"
        self._state_commits.append(
            {"sha": sha, "message": message, "files": list(files)}
        )
        return sha

    async def state_read(self, paths: list[str]) -> dict[str, bytes | None]:
        """Return the in-memory content for the requested paths."""
        return {p: self._state_files.get(p) for p in paths}

    async def state_history(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return in-memory commit history (newest first, up to *limit*).

        Mirrors the real runtime's *flat* commit shape: the
        ``X-Primer-Session`` / ``X-Primer-Agent`` / ``X-Primer-Op`` trailers
        embedded in the commit message are parsed out into top-level
        ``session_id`` / ``agent_id`` / ``op`` fields, and the optional
        ``session_id`` / ``agent_id`` filters are honoured. This keeps the
        stub faithful enough for session-rehydration tests that drive
        ``create_session`` and then expect ``list_session_ids`` to recover
        the persisted ids.
        """
        out: list[dict] = []
        for commit in reversed(self._state_commits):
            trailers = _parse_trailers(commit.get("message", ""))
            sid = trailers.get("X-Primer-Session")
            aid = trailers.get("X-Primer-Agent")
            if session_id is not None and sid != session_id:
                continue
            if agent_id is not None and aid != agent_id:
                continue
            enriched = dict(commit)
            if sid is not None:
                enriched["session_id"] = sid
            if aid is not None:
                enriched["agent_id"] = aid
            if "X-Primer-Workspace" in trailers:
                enriched["workspace_id"] = trailers["X-Primer-Workspace"]
            if "X-Primer-Op" in trailers:
                enriched["op"] = trailers["X-Primer-Op"]
            out.append(enriched)
            if len(out) >= limit:
                break
        return out


__all__ = ["FakeSandbox"]
