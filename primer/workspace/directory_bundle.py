"""Bounded service archive capture. Both distributions carry this standalone module; CI checks parity."""

from __future__ import annotations

import io
import os
import stat
import tarfile
from contextlib import ExitStack
from pathlib import PurePosixPath


MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 200
MAX_MEMBERS = 1000
# Includes file padding, up to 200 extended path headers, and gzip framing.
MAX_ARCHIVE_BYTES = MAX_BYTES + 2 * 1024 * 1024


def capture_directory(root: str | os.PathLike[str], path: str, reserved: tuple[str, ...]) -> bytes:
    """Tar the contents of a dedicated workspace subdirectory, relative to its root.

    Open each component beneath a pinned parent descriptor. Never follow a link
    or read more than the remaining budget if the workspace changes during capture.
    """
    if not path or path.startswith("/") or "\\" in path or "\x00" in path or ".." in path.split("/"):
        raise ValueError("expected a workspace-relative bundle directory")
    directory = PurePosixPath(path)
    if not directory.parts:
        raise ValueError("select a dedicated bundle directory inside the workspace")
    folded = PurePosixPath(str(directory).casefold())
    protected = [PurePosixPath(p.strip("/").casefold()) for p in reserved]
    if ".git" in folded.parts or any(folded.is_relative_to(p) or p.is_relative_to(folded) for p in protected):
        raise ValueError("bundle directory overlaps a reserved workspace tree")
    if len(directory.parts) > 64 or len(os.fsencode(path)) > 4096:
        raise ValueError("bundle directory path is too long")
    if os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("this workspace cannot safely capture a directory")
    dir_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    members = files = total = 0
    output = io.BytesIO()

    def walk(parent: int, prefix: str, depth: int, archive: tarfile.TarFile) -> None:
        nonlocal members, files, total
        if depth > 64:
            raise ValueError("bundle directory nesting exceeds 64 levels")
        with os.scandir(parent) as entries:
            for entry in entries:
                members += 1
                if members > MAX_MEMBERS:
                    raise ValueError(f"bundle exceeds {MAX_MEMBERS} members")
                name = f"{prefix}{entry.name}"
                if entry.name.casefold() == ".git" or "\\" in name or len(os.fsencode(name)) > 4096:
                    raise ValueError("unsupported bundle member path")
                flags = dir_flags if entry.is_dir(follow_symlinks=False) else os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                # Refuse devices and special files before open; check the opened descriptor again.
                if flags != dir_flags and not entry.is_file(follow_symlinks=False):
                    raise ValueError(f"bundle member {name!r} must be a regular file or directory")
                fd = os.open(entry.name, flags, dir_fd=parent)
                try:
                    st = os.fstat(fd)
                    if stat.S_ISDIR(st.st_mode):
                        walk(fd, name + "/", depth + 1, archive)
                        continue
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                        raise ValueError(f"bundle member {name!r} must be a regular file with a single link")
                    files += 1
                    if files > MAX_FILES or st.st_size > MAX_BYTES - total:
                        raise ValueError("bundle exceeds the file count or total byte limit")
                    info = tarfile.TarInfo(name)
                    info.size = st.st_size
                    with os.fdopen(fd, "rb", closefd=False) as source:
                        archive.addfile(info, source)
                        if source.read(1) or os.fstat(fd).st_mtime_ns != st.st_mtime_ns:
                            raise ValueError(f"bundle file {name!r} changed during capture")
                    total += st.st_size
                finally:
                    os.close(fd)

    with ExitStack() as stack:
        parent = os.open(root, dir_flags)
        stack.callback(os.close, parent)
        for part in directory.parts:
            parent = os.open(part, dir_flags, dir_fd=parent)
            stack.callback(os.close, parent)
        with tarfile.open(fileobj=output, mode="w:gz", compresslevel=1) as archive:
            walk(parent, "", 0, archive)
    if not files:
        raise ValueError("bundle directory contains no files")
    if output.tell() > MAX_ARCHIVE_BYTES:
        raise ValueError("bundle archive exceeds the transfer limit")
    return output.getvalue()
