"""Generic agent-runtime child process layer.

Helpers for adapters that run a CLI step in a short-lived child process. The
adapter supplies its own directory prefix, files, environment and arguments.

Three guarantees:

1. The child gets a MINIMAL environment (:func:`child_env`): a fixed allowlist plus
   the operator-named ``pass_env`` names plus the provider-named ``extra`` entries,
   nothing else, so a worker holding database or proxy credentials does not leak
   them to a process it shells out to.
2. The child runs in its own session and process group (``primer.common.process_group``)
   with its temp directory recorded, so normal exit, a Stop, a timeout or a cancel all
   funnel into ONE cleanup: SIGKILL the whole group, then remove the directory. The
   cleanup is shielded (``primer.common.shielded``), so it survives the caller being
   cancelled a second time while it runs. On Linux the child also asks the kernel
   (``prctl(PR_SET_PDEATHSIG, SIGKILL)``, when the parent could resolve ``prctl``)
   to kill it if the WORKER dies first, without waiting for the crash sweep.
3. A crashed worker (killed -9, power loss) leaves its temp directory behind.
   :func:`sweep_orphans` is the backstop: at worker start it removes
   ``<prefix>-<pid>-*`` directories whose worker pid is gone, killing the
   process group the directory's ``child.json`` records when the record is
   usable. It trusts only a directory it can verify is its own (a 0700
   directory of the sweep's uid, no symlink): a directory WITHOUT a usable
   record is still deleted (the normal crash-between-mkdtemp-and-write case),
   while a directory with a TAMPERED record (a symlinked or foreign
   ``child.json``, or a record pointing at the sweep's own group) is neither
   killed nor deleted, so a name planted in the shared temp root cannot steer
   the sweep at a foreign process group.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import logging
import os
import shutil
import signal
import stat
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from primer.common import shielded
from primer.common.process_group import REAP_TIMEOUT_S, NEW_SESSION, kill_process_group

logger = logging.getLogger(__name__)

#: The only worker environment variables a child may see, plus the operator-named
#: ``pass_env`` names and the provider-named ``extra`` entries. Everything else is dropped.
ALLOWED: frozenset[str] = frozenset(
    {"HOME", "PATH", "USER", "LOGNAME", "LANG", "TERM", "SHELL", "TMPDIR"}
)

#: The child's stdout readline limit. asyncio's default (64 KiB) raises
#: ``ValueError`` on any longer line, and the CLI's ``assistant``/``result`` lines
#: carry whole content blocks -- a long answer would kill the step.
_STREAM_LINE_LIMIT = 2**26

#: How long ``finally`` waits for the shielded cleanup. ``kill_process_group`` is bounded
#: by ``REAP_TIMEOUT_S`` internally; the margin covers the directory removal.
CLEANUP_WAIT_S = REAP_TIMEOUT_S + 1.0

#: ``prctl`` option number for ``PR_SET_PDEATHSIG`` (man 2 prctl).
_PR_SET_PDEATHSIG = 1


def _resolve_pdeathsig() -> Callable[[], None] | None:
    """Bind libc's ``prctl`` ONCE in the parent, before any fork; ``None`` when it
    cannot be resolved (off Linux, or no ``prctl`` symbol).

    A forked child must not dlopen (unsafe mid-fork, and a named ``libc.so.6``
    does not exist on musl), so ``ctypes.CDLL(None)`` -- the process's own global
    handle -- is used here, in the parent. The returned hook calls only the
    already-bound function and tolerates failure: a death signal that could not be
    set is not fatal, the crash sweep is the backstop.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        prctl = ctypes.CDLL(None).prctl
    except (OSError, AttributeError):
        return None

    def _set() -> None:
        try:
            prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
        except OSError:
            pass  # not fatal: the orphan sweep is the backstop

    return _set


#: The preexec_fn for Linux children, or ``None`` (no death signal set; the sweep
#: then finds the child's directory after the worker dies).
_PDEATHSIG = _resolve_pdeathsig()


def child_env(pass_env: Sequence[str], *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the child environment: the :data:`ALLOWED` names, the ``pass_env`` names
    (only if set in the worker), and the ``extra`` entries. Nothing else."""
    env = {name: os.environ[name] for name in ALLOWED if name in os.environ}
    for name in pass_env:
        if name in os.environ:
            env[name] = os.environ[name]
    if extra:
        env.update(extra)
    return env


async def _cleanup(proc: asyncio.subprocess.Process, tmp: Path) -> None:
    """SIGKILL the child's process group, then remove its temp directory."""
    try:
        await kill_process_group(proc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _require_leaf_name(name: str) -> None:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("expected a single filename component")


@asynccontextmanager
async def child_process(
    argv_without_tmp: Callable[[Path], list[str]],
    *,
    env: dict[str, str],
    prefix: str,
    files: Mapping[str, str] | None = None,
    line_limit: int = _STREAM_LINE_LIMIT,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Run one agent-runtime child for the lifetime of the ``async with`` block.

    Creates a 0700 temp directory named ``<prefix>-<worker pid>-*`` holding the
    ``files`` (name to text, e.g. the system prompt's ``system.md``) and, once
    the child is up, ``child.json`` (``{"pid": int, "pgid": int}``); spawns
    ``argv_without_tmp(tmp)`` IN THAT DIRECTORY (spec 6.2 step 2: the temp dir
    is the child's working directory) in its own session and process group
    (plus the Linux ``prctl`` death signal when resolvable); and, on ANY way out
    of the block (normal return, an error, a failed spawn, a cancel), kills the
    whole process group and removes the directory. The cleanup runs shielded, so
    a second cancel of the caller cannot leave it half done. ``line_limit`` is
    the child's stdout readline limit (``_STREAM_LINE_LIMIT`` by default).

    The child's stderr goes to ``stderr.log`` in the temp dir (a file, not a
    pipe): nobody drains pipes while a stream is being read, so a chatty child
    would block after ~64 KiB; the file is quoted via :func:`read_stderr_tail`.
    Stdin and stdout stay pipes; the provider reads stdout.

    ``argv_without_tmp`` receives the temp directory so the caller can point its
    ``--system-prompt-file`` (and any other path it needs) at it.
    """
    _require_leaf_name(prefix)
    tmp = Path(tempfile.mkdtemp(prefix=f"{prefix}-{os.getpid()}-"))
    proc: asyncio.subprocess.Process | None = None
    try:
        os.chmod(tmp, 0o700)  # mkdtemp already uses 0700; keep it explicit and umask-proof
        for name, text in (files or {}).items():
            _require_leaf_name(name)
            if name in {"stderr.log", "child.json"}:
                raise ValueError(f"{name} is reserved for child process bookkeeping")
            (tmp / name).write_text(text, encoding="utf-8")
        stderr_log = (tmp / "stderr.log").open("wb")
        kwargs: dict[str, object] = dict(NEW_SESSION)
        if _PDEATHSIG is not None:
            # preexec_fn runs between fork and exec in a (threaded) worker: it must stay
            # async-signal-safe (the single prctl call is), or a second thread can deadlock the fork.
            kwargs["preexec_fn"] = _PDEATHSIG
        proc = await asyncio.create_subprocess_exec(
            *argv_without_tmp(tmp),
            env=env,
            cwd=tmp,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_log,
            limit=line_limit,
            **kwargs,
        )
        # ``start_new_session`` makes the child the leader of a new group whose id is
        # its pid, so ``pgid == pid``; the crash sweep reads this back.
        (tmp / "child.json").write_text(
            json.dumps({"pid": proc.pid, "pgid": proc.pid}), encoding="utf-8"
        )
        yield proc
    finally:
        if proc is not None:
            # Shielded: if this task is cancelled again while the cleanup runs, the
            # cleanup keeps running to the end (primer.common.shielded holds it).
            await shielded.run_in_background(
                _cleanup(proc, tmp),
                what=f"agent-runtime child {proc.pid}",
                verb="cleanup",
                wait_s=CLEANUP_WAIT_S,
            )
        # Reached when the spawn (or the writes above) failed too: nothing to kill.
        shutil.rmtree(tmp, ignore_errors=True)


def read_stderr_tail(proc_tmpdir: Path, limit: int = 4000) -> str:
    """The last ``limit`` bytes of the child's ``stderr.log`` ("" when missing).

    The child's stderr goes to that file (see :func:`child_process`); a later
    task quotes it in error messages. Decoded leniently: stderr may be anything.
    """
    try:
        data = (proc_tmpdir / "stderr.log").read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def _worker_pid_from_dir(name: str, prefix: str) -> int | None:
    """The worker pid encoded in a ``<prefix>-<pid>-<suffix>`` name, or ``None``.

    A matching prefix is required, not just the shape: any directory with a numeric
    dash-separated segment (``my-project-2024-backup``) would otherwise parse as
    a worker pid, and a sweep would SIGKILL that group and rmtree the directory.
    """
    marker = f"{prefix}-"
    if name.startswith(marker):
        head = name[len(marker):].split("-")[0]
        if head.isdigit():
            return int(head)
    return None


def _worker_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # PermissionError: the pid belongs to somebody else; treat as alive
    return True


@dataclass(frozen=True)
class _ChildRecord:
    """One dead worker's ``child.json``, tri-state: the sweep's decision.

    ``pgid``: the process group to SIGKILL, or ``None`` when there is nothing
    to kill (no record at all, an unusable one, or a group that must not be
    signalled). ``tampered``: a real tampering signal (a symlinked or foreign
    record, or a record pointing at our own group) -- the directory is LEFT
    ALONE; otherwise the sweep removes it, with the kill only when ``pgid`` is
    set.
    """

    pgid: int | None
    tampered: bool = False


def _child_pgid(child_json: Path) -> _ChildRecord:
    """Read ``child.json`` and decide the sweep's action for its directory.

    The file is opened with ``O_NOFOLLOW`` and inspected with ``fstat`` on the
    open fd (one atomic check, not lstat-then-read): a SYMLINK or any file
    that is not a regular file of this uid is a tampering signal (leave the
    directory alone), as is a record pointing at THIS process's own group
    (planted or corrupted, never one we wrote). A MISSING or unreadable file,
    an unparseable record, or a recorded pgid that must not be signalled
    (``killpg(0)`` means "our own group", 1 is init) is the
    crash-between-mkdtemp-and-write / partial-write case: nothing is
    signalled, but the directory is ours, so the sweep deletes it.
    """
    try:
        fd = os.open(child_json, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return _ChildRecord(None, True)  # child.json is a symlink: tampered
        return _ChildRecord(None, False)  # missing or unreadable: nothing to kill
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        return _ChildRecord(None, True)  # not our regular file: tampered
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        try:
            data = json.loads(handle.read())
            pgid = int(data["pgid"])
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return _ChildRecord(None, False)  # no usable record: nothing to kill
    if pgid <= 1:
        return _ChildRecord(None, False)  # killpg(0) = our own group, 1 = init
    if pgid == os.getpgrp():
        return _ChildRecord(None, True)  # pointing at our own group: tampered
    return _ChildRecord(pgid, False)


async def sweep_orphans(
    tmp_root: Path | None = None,
    *,
    prefix: str,
) -> int:
    """Kill and remove agent-runtime temp directories whose worker is dead.

    Scans ``tmp_root`` (``tempfile.gettempdir()`` by default) for
    ``<prefix>-<pid>-*`` directories of the calling provider. A directory is swept when the worker pid in
    its name no longer exists (``os.kill(pid, 0)`` raising ``ProcessLookupError``)
    and it is one the worker created itself (a real directory -- a symlink is not
    one, checked with ``lstat`` -- owned by the sweep's uid with mode 0700).
    Sweeping sends SIGKILL to the process group recorded in the directory's
    ``child.json`` (``ProcessLookupError`` ignored: the group may already be gone)
    and then removes the directory. A directory WITHOUT a usable record (no
    ``child.json``, an unreadable or unparseable one, or a pgid that must not be
    signalled) is removed WITHOUT a kill and still counted: that is the normal
    crash-between-mkdtemp-and-write case. A directory with a TAMPERED record
    (``child.json`` a symlink or not a regular file, a record of another uid, or
    a record pointing at our own group) is NEITHER killed NOR deleted: the shared
    temp root lets anyone plant a ``<prefix>-<deadpid>-*`` name, so such a
    directory is only logged and skipped. Directories of a LIVE worker are left
    alone. Returns how many it cleaned.

    Accepted edges, the same class ``kill_process_group`` documents: (1) the
    WORKER pid in a directory name could have been recycled to an unrelated
    live process; the liveness check then treats the worker as alive and the
    directory is left alone (a leaked directory, never a stray kill); (2) the
    child pgid in ``child.json`` could in theory be recycled to an unrelated
    group before the sweep runs; that needs a pid wrap around a dead group.
    """
    _require_leaf_name(prefix)
    root = tmp_root if tmp_root is not None else Path(tempfile.gettempdir())
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    cleaned = 0
    for entry in entries:
        pid = _worker_pid_from_dir(entry.name, prefix)
        if pid is None or not entry.is_dir() or _worker_is_alive(pid):
            continue
        # Trust the directory only when it is one WE created: a real directory
        # (a symlink is not one -- lstat), owned by this uid, mode 0700. The temp
        # root is shared: anyone who can write to it could plant a
        # ``<prefix>-<deadpid>-*`` name and a forged ``child.json`` pointing at an
        # arbitrary process group of this uid. An untrusted directory is neither
        # killed nor deleted, only logged and skipped.
        try:
            st = entry.lstat()
        except OSError:
            continue
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o700
        ):
            logger.warning(
                "crash sweep: %s is not a 0700 directory of this uid; leaving it", entry.name,
            )
            continue
        record = _child_pgid(entry / "child.json")
        if record.tampered:
            logger.warning("crash sweep: %s has a tampered child.json; leaving it", entry.name)
            continue
        if record.pgid is not None:
            try:
                os.killpg(record.pgid, signal.SIGKILL)
            except OSError:
                pass  # ProcessLookupError: the group is already gone
        # No usable record is the normal crash-between-mkdtemp-and-write case:
        # nothing is signalled, but the trusted directory is ours and removed.
        shutil.rmtree(entry, ignore_errors=True)
        cleaned += 1
    return cleaned
