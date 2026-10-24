"""The filesystem artifact store: artifact files in a folder that every server pod mounts at the same path (plan 09, design section 15.2).

Layout. Each artifact is two files under ``<root>/<hex[0:2]>/<hex[2:4]>/``: the bytes at ``<id>`` and a UTF-8 JSON sidecar at ``<id>.json``
(``{"mime_type", "filename", "size"}``). Each is written to a temporary file, synced and renamed into place, the bytes first, so a sidecar is never
seen without its bytes.

The root marker. Preparing a root (:func:`prepare_root`: on a create or an edit of the provider, and by a draft Test) creates the folder and writes
``.primer-artifact-root`` in it. Every process requires the marker before it uses the store (:func:`check_root`, run by ``initialize``). A pod
that lacks the shared volume finds either no folder or an empty folder on its own disk, refuses, and says to mount the volume: it never writes
files that the other pods cannot read.

Paths never leave the root. An id must match :data:`~primer.artifact.ids.BACKEND_ARTIFACT_ID_RE` before it is turned into a path (``get`` and
``delete`` of any other id log a warning and do nothing), so it holds no separator, no ``..`` and cannot be absolute. Below the root, each shard
folder must be a real folder (a symlink is refused) and must resolve inside the resolved root, and the two files are opened with ``O_NOFOLLOW``,
so a planted link reads, writes and deletes nothing outside the root. The root itself may be a symlink (a mount reached through a link).

Every file operation runs in a worker thread (``asyncio.to_thread``): a slow or hung volume never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import ClassVar

from primer.artifact.ids import is_backend_artifact_id, new_backend_artifact_id
from primer.int.artifact_storage import ArtifactBlob, ArtifactStorage
from primer.model.except_ import ConfigError

logger = logging.getLogger(__name__)

#: The file a prepared root holds. Its presence is what tells a process that the folder at ``root`` is the one the provider was prepared on.
ROOT_MARKER = ".primer-artifact-root"

_MARKER_TEXT = (
    b"This folder holds primer's artifact files. Every primer server pod (the API and every worker) mounts it at the same path.\n"
    b"Do not edit or remove what it holds.\n"
)
_SIDECAR_SUFFIX = ".json"
# O_NONBLOCK keeps an open of a planted FIFO from hanging the thread; it changes nothing for a regular file.
_OPEN_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_OPEN_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafeArtifactPath(ValueError):
    """A path below the root that could lead outside it: a shard folder that is a link or a file, a folder that resolves elsewhere, or a file that
    is a link or not a regular file."""


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _require_absolute(root: str) -> None:
    if not isinstance(root, str) or not root or not os.path.isabs(root):
        raise ConfigError(f"artifact root {root!r} must be an absolute path")
    # A NUL makes every os call raise a bare ValueError (a 500 through the REST route), so it is refused here, by name.
    if "\x00" in root:
        raise ConfigError(f"artifact root {root!r} must not contain a NUL character")


def _not_writable(root: str, exc: OSError) -> ConfigError:
    return ConfigError(f"artifact root {root!r} is not writable: {_reason(exc)}")


def _write_probe(root: str) -> None:
    """Create, write, sync and remove a temporary file in ``root``."""
    name: str | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".primer-write-probe-", suffix=".tmp", dir=root)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"primer write probe")
            handle.flush()
            os.fsync(handle.fileno())
        os.unlink(name)
        name = None
    except OSError as exc:
        raise _not_writable(root, exc) from exc
    finally:
        if name is not None:
            with contextlib.suppress(OSError):
                os.unlink(name)


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write ``payload`` to a new temporary file next to ``path``, sync it and rename it over ``path``. The temporary file never follows a link."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, _OPEN_NEW, 0o666)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def prepare_root(root: str) -> None:
    """Make ``root`` usable: create it when missing, check that it takes a write, and write the marker when it has none.

    Run when a provider is created or edited, and by a draft Test. Raises :class:`ConfigError` naming the root.
    """
    _require_absolute(root)
    if os.path.lexists(root) and not os.path.isdir(root):
        raise ConfigError(f"artifact root {root!r} is not a directory")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        raise _not_writable(root, exc) from exc
    _write_probe(root)
    marker = Path(root) / ROOT_MARKER
    if not marker.is_file():
        try:
            _write_atomic(marker, _MARKER_TEXT)
        except OSError as exc:
            raise _not_writable(root, exc) from exc


def check_root(root: str) -> None:
    """Refuse a root this process cannot use: not absolute, missing, not a folder, never prepared (no marker), or not writable.

    It creates nothing: a missing root or a missing marker means this process does not see the shared volume. Raises :class:`ConfigError`
    naming the root.
    """
    _require_absolute(root)
    if not os.path.exists(root):
        raise ConfigError(
            f"artifact root {root!r} does not exist in this process; mount the shared artifact volume at this path in every server pod "
            "(the API and every worker)"
        )
    if not os.path.isdir(root):
        raise ConfigError(f"artifact root {root!r} is not a directory")
    if not (Path(root) / ROOT_MARKER).is_file():
        raise ConfigError(
            f"artifact root {root!r} has no {ROOT_MARKER} marker in this process, so it is not the folder the provider was prepared on; "
            "mount the shared artifact volume here in every server pod, or save the provider again to prepare the folder"
        )
    _write_probe(root)


def _sidecar_of(path: Path) -> Path:
    return path.with_name(path.name + _SIDECAR_SUFFIX)


def _shard_dir(root: str, path: Path, *, create: bool) -> Path | None:
    """The folder that holds ``path``, checked: each shard folder below the root is a real folder, never a link, and the folder resolves inside
    the resolved root. With ``create`` the shard folders are made; without it a missing one answers ``None`` (nothing is stored there)."""
    current = Path(root)
    for part in path.parent.relative_to(root).parts:
        current = current / part
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(current)
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if create:
                raise
            return None
        if not stat.S_ISDIR(mode):
            raise UnsafeArtifactPath(f"{str(current)!r} is not a folder; a link there could lead outside the artifact root {root!r}")
    resolved_root = os.path.realpath(root)
    resolved = os.path.realpath(current)
    if os.path.commonpath([resolved_root, resolved]) != resolved_root:
        raise UnsafeArtifactPath(f"{str(current)!r} resolves to {resolved!r}, outside the artifact root {root!r}")
    return current


def _read_nofollow(path: Path) -> bytes:
    try:
        fd = os.open(path, _OPEN_READ)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeArtifactPath(f"{str(path)!r} is a link; artifact files are never read through a link") from exc
        raise
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise UnsafeArtifactPath(f"{str(path)!r} is not a regular file")
        return handle.read()


def _write_files(root: str, path: Path, data: bytes, sidecar: bytes) -> None:
    """Write the bytes, then the sidecar. A sidecar that fails removes the bytes again."""
    _shard_dir(root, path, create=True)
    _write_atomic(path, data)
    try:
        _write_atomic(_sidecar_of(path), sidecar)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def _read_files(root: str, path: Path) -> ArtifactBlob | None:
    """The stored blob, or ``None`` when the sidecar or the bytes are missing, or when a path below the root is unsafe (logged)."""
    try:
        if _shard_dir(root, path, create=False) is None:
            return None
        sidecar = _read_nofollow(_sidecar_of(path))
        data = _read_nofollow(path)
    except FileNotFoundError:
        return None
    except UnsafeArtifactPath as exc:
        logger.warning("filesystem artifact store: refused to read %s: %s", path.name, exc)
        return None
    meta = json.loads(sidecar.decode("utf-8"))
    return ArtifactBlob(data=data, mime_type=meta["mime_type"], filename=meta.get("filename"))


def _remove_files(root: str, path: Path) -> None:
    """Remove the sidecar, then the bytes; missing files are fine. ``unlink`` removes a link itself, never the file it points at."""
    try:
        if _shard_dir(root, path, create=False) is None:
            return
    except UnsafeArtifactPath as exc:
        logger.warning("filesystem artifact store: refused to delete %s: %s", path.name, exc)
        return
    for target in (_sidecar_of(path), path):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(target)


class FilesystemArtifactStorage(ArtifactStorage):
    """Artifacts as files under ``root``, a folder shared by every server pod. Its ids are not database ids, so the routing store records them."""

    needs_location_record: ClassVar[bool] = True

    def __init__(self, root: str) -> None:
        # No IO here: the factory builds stores for rows whose root this process may not see (the checks and tests build with fake roots).
        self._root = root

    @property
    def root(self) -> str:
        return self._root

    async def initialize(self) -> None:
        await asyncio.to_thread(check_root, self._root)

    def path_for(self, artifact_id: str) -> Path:
        """``root/<hex[0:2]>/<hex[2:4]>/<id>``. Raises ``ValueError`` for an id that is not a backend artifact id, before any path is built."""
        if not is_backend_artifact_id(artifact_id):
            raise ValueError(f"{artifact_id!r} is not a backend artifact id")
        hexpart = artifact_id.removeprefix("artifact-")
        return Path(self._root) / hexpart[:2] / hexpart[2:4] / artifact_id

    def _path_or_none(self, artifact_id: str, verb: str) -> Path | None:
        try:
            return self.path_for(artifact_id)
        except ValueError:
            logger.warning("filesystem artifact store %r: refused to %s %r, which is not a backend artifact id", self._root, verb, artifact_id)
            return None

    async def put(self, *, data: bytes, mime_type: str, filename: str | None = None) -> str:
        artifact_id = new_backend_artifact_id()
        sidecar = json.dumps({"mime_type": mime_type, "filename": filename, "size": len(data)}, ensure_ascii=False).encode("utf-8")
        await asyncio.to_thread(_write_files, self._root, self.path_for(artifact_id), data, sidecar)
        return artifact_id

    async def get(self, artifact_id: str) -> ArtifactBlob | None:
        path = self._path_or_none(artifact_id, "read")
        if path is None:
            return None
        return await asyncio.to_thread(_read_files, self._root, path)

    async def delete(self, artifact_id: str) -> None:
        path = self._path_or_none(artifact_id, "delete")
        if path is None:
            return
        await asyncio.to_thread(_remove_files, self._root, path)

    async def aclose(self) -> None:
        return None


__all__ = ["ROOT_MARKER", "FilesystemArtifactStorage", "UnsafeArtifactPath", "check_root", "prepare_root"]
