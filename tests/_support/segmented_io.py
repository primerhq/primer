"""A mixin for the in-memory workspace IO fakes that serve a session log: the listing calls the log facade (``SessionLog`` over ``WorkspaceSource``) makes.

A fake keeps its files as ``{path: bytes}`` (``self._files``, ``self.files``, or what ``_log_files`` returns) and already has ``read_file``; the mixin adds
``list_files`` (the files directly under a directory), ``file_info`` (a size) and, unless the fake has its own, ``read_file_from`` (the whole file sliced).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from primer.model.except_ import NotFoundError


class ListsFiles:
    def _log_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] | None = getattr(self, "files", None)
        return files if files is not None else self._files

    async def list_files(self, path: str = ".", **_kw: Any) -> list[Any]:
        prefix = path.rstrip("/") + "/"
        return [
            SimpleNamespace(path=p, kind="file", size_bytes=len(data))
            for p, data in self._log_files().items()
            if p.startswith(prefix) and "/" not in p[len(prefix) :]
        ]

    async def file_info(self, path: str) -> Any:
        files = self._log_files()
        if path not in files:
            raise NotFoundError(f"{path!r} not found")
        return SimpleNamespace(size_bytes=len(files[path]))

    async def read_file_from(self, path: str, offset: int) -> bytes:
        return (await self.read_file(path))[offset:]  # type: ignore[attr-defined]
