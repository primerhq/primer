"""LocalWorkspaceBackend — workspace provider backed by ordinary host dirs.

Each workspace is materialised under ``<root>/<workspace_id>/``.
Workspaces materialised in one process are tracked in memory only;
provider re-discovery on restart is a future enhancement.

Per the spec, this backend skips capabilities it cannot enforce:

* Resource limits (CPU / memory / disk) — startup warning if any set.
* Network mode — startup warning, no enforcement.
* Package installation — init_commands still run.

File sources of all kinds (``inline``, ``url``, ``document``, ``secret``)
are resolved by :func:`primer.workspace.files.resolve_file_sources`
before they are written to the workspace directory.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` and
``docs/superpowers/specs/2026-05-11-workspace-backends-design.md`` §12.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import uuid
from pathlib import Path

from primer.int.workspace import Workspace
from primer.model.except_ import BadRequestError, NotFoundError, SubprocessTimeoutError
from primer.model.workspace import (
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace.base_backend import BaseWorkspaceBackend
from primer.workspace.files import FileResolvers, ResolvedFile
from primer.workspace.local.workspace import LocalWorkspace


logger = logging.getLogger(__name__)


def _generate_workspace_id() -> str:
    return f"ws-{uuid.uuid4().hex[:16]}"


class LocalWorkspaceBackend(BaseWorkspaceBackend):
    """:class:`WorkspaceProvider` backed by ordinary directories on disk.

    Stores every workspace under ``<root>/<workspace_id>/``. Workspaces
    materialised in one process are tracked in memory only; provider
    re-discovery on restart is a future enhancement.
    """

    def __init__(
        self,
        root: Path,
        *,
        subprocess_timeout_seconds: float = 120.0,
    ) -> None:
        super().__init__()
        self._root = Path(root)
        self._subprocess_timeout_seconds = subprocess_timeout_seconds
        self._workspaces: dict[str, LocalWorkspace] = {}

    @property
    def root(self) -> Path:
        return self._root

    async def initialize(self) -> None:
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        self._initialised = True

    async def aclose(self) -> None:
        async with self._lock:
            for ws in list(self._workspaces.values()):
                try:
                    await ws.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "LocalWorkspaceBackend: aclose on workspace failed",
                        extra={"workspace_id": ws.id, "error": str(exc)},
                    )
            self._workspaces.clear()
            self._initialised = False

    async def create(
        self,
        template: WorkspaceTemplate,
        *,
        overrides: WorkspaceTemplateOverrides | None = None,
        resolvers: FileResolvers | None = None,
    ) -> Workspace:
        if not self._initialised:
            await self.initialize()

        # Warn on capabilities we cannot enforce; do NOT fail.
        _warn_unenforced(template)

        # Merge template + overrides (merge-then-extend semantics).
        merged = self.merge_overrides(template, overrides)
        env_str = merged.env_unwrapped()

        workspace_id = _generate_workspace_id()
        ws_root = self._root / workspace_id
        await asyncio.to_thread(ws_root.mkdir, parents=True, exist_ok=False)

        try:
            # Resolve every FileSource variant (inline/url/document/secret)
            # up-front via the shared helper; the backend just writes the
            # resulting bytes to disk.
            await self.materialize_files_on_backend(
                merged.files,
                lambda rf: self._materialise_resolved_file(ws_root, rf),
                resolvers=resolvers,
            )
            for cmd in merged.init_commands:
                await self._run_init_command(ws_root, cmd, env_str)
            ws = await LocalWorkspace.materialise(
                workspace_id=workspace_id,
                root=ws_root,
                template=template,
                env=env_str,
                subprocess_timeout_seconds=self._subprocess_timeout_seconds,
            )
        except Exception:
            # Roll back the partially-built workspace directory so a
            # retry sees a clean root.
            try:
                await asyncio.to_thread(shutil.rmtree, ws_root)
            except Exception:  # noqa: BLE001
                pass
            raise

        async with self._lock:
            self._workspaces[workspace_id] = ws
        return ws

    async def _reattach(
        self,
        workspace_id: str,
        template: WorkspaceTemplate | None,
    ) -> Workspace | None:
        """Re-attach a :class:`LocalWorkspace` from disk after a cache miss.

        The on-disk directory ``<root>/<workspace_id>/`` survives api
        restarts; rebuilding the in-memory ``LocalWorkspace`` from it
        is what keeps existing workspaces usable after a process bounce.
        Re-attach requires the template (so we know the state/tmp
        sub-paths and the env to re-derive); when the caller doesn't
        supply one and the workspace isn't already in the in-memory
        cache, we cannot safely re-attach and return ``None``.

        Called by :meth:`BaseWorkspaceBackend.get` only after the cache
        lookup (with gone-eviction) misses.
        """
        if not self._initialised:
            await self.initialize()
        ws_root = self._root / workspace_id
        if not await asyncio.to_thread(ws_root.is_dir):
            return None
        if template is None:
            logger.warning(
                "LocalWorkspaceBackend.get: workspace %s exists on disk "
                "but no template was provided; re-attach skipped",
                workspace_id,
            )
            return None
        env_str = {k: v.get_secret_value() for k, v in template.env.items()}
        try:
            ws = await LocalWorkspace.materialise(
                workspace_id=workspace_id,
                root=ws_root,
                template=template,
                env=env_str,
                subprocess_timeout_seconds=self._subprocess_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LocalWorkspaceBackend.get: re-attach failed for %s: %s",
                workspace_id, exc,
            )
            return None
        async with self._lock:
            # Re-check after the lock: a concurrent caller may have
            # materialised the same workspace while we were rebuilding.
            existing = self._workspaces.get(workspace_id)
            if existing is not None:
                # Drop our rebuild; the existing handle wins.
                try:
                    await ws.aclose()
                except Exception:  # noqa: BLE001
                    pass
                return existing
            self._workspaces[workspace_id] = ws
        logger.info(
            "LocalWorkspaceBackend: re-attached workspace %s from disk",
            workspace_id,
        )
        return ws

    async def list(self) -> list[str]:
        return list(self._workspaces)

    async def destroy(self, workspace_id: str) -> None:
        async with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
        if ws is None:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        try:
            await ws.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LocalWorkspaceBackend: aclose on destroy failed",
                extra={"workspace_id": workspace_id, "error": str(exc)},
            )
        await asyncio.to_thread(shutil.rmtree, ws.root, ignore_errors=True)

    # ---- internals ------------------------------------------------------

    async def _materialise_resolved_file(
        self, ws_root: Path, rf: ResolvedFile,
    ) -> None:
        if "\x00" in rf.path:
            raise BadRequestError(f"file path contains null byte: {rf.path!r}")
        target = ws_root / rf.path
        # Defensive: keep writes inside ws_root.
        try:
            target.resolve().relative_to(ws_root.resolve())
        except ValueError as exc:
            raise BadRequestError(
                f"file path resolves outside workspace: {rf.path!r}"
            ) from exc
        await asyncio.to_thread(
            target.parent.mkdir, parents=True, exist_ok=True
        )
        await asyncio.to_thread(target.write_bytes, rf.content)
        if rf.mode is not None:
            try:
                octal = int(rf.mode, 8)
                await asyncio.to_thread(target.chmod, octal)
            except (ValueError, OSError, NotImplementedError):
                # Mode application is best-effort on local backend.
                pass

    async def _run_init_command(
        self,
        ws_root: Path,
        command: str,
        env: dict[str, str],
    ) -> None:
        # Same curation rule as the Exec tool: only safelisted parent
        # variables, plus the workspace template's own env. Do NOT
        # inherit the API server's full environment (would leak DB +
        # provider credentials to the init shell).
        from primer.workspace.local.tools.exec_ import _curated_subprocess_env
        proc_env = _curated_subprocess_env()
        if env:
            proc_env.update(env)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ws_root),
            env=proc_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._subprocess_timeout_seconds,
            )
        except TimeoutError as exc:
            # Kill the entire process group so child processes spawned by
            # the shell (e.g. a "sleep" inside "apt-get install") don't
            # keep the pipes open and cause proc.wait() to hang.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                pass
            raise SubprocessTimeoutError(
                f"init_command timed out after "
                f"{self._subprocess_timeout_seconds}s: {command!r}"
            ) from exc
        if proc.returncode != 0:
            raise BadRequestError(
                f"init command failed (rc={proc.returncode}): {command!r}\n"
                f"stderr: {stderr.decode('utf-8', errors='replace').strip()}"
            )
        del stdout  # success path does not surface stdout


# ===========================================================================
# Helpers
# ===========================================================================


def _warn_unenforced(template: WorkspaceTemplate) -> None:
    """Emit warnings for template features the local backend cannot enforce."""
    r = template.resources
    if (
        r.cpu_cores is not None
        or r.memory_bytes is not None
        or r.disk_bytes is not None
    ):
        logger.warning(
            "LocalWorkspaceBackend does not enforce resource limits"
        )
    if r.network != "egress":
        logger.warning(
            "LocalWorkspaceBackend does not enforce network mode",
            extra={"network": r.network},
        )


__all__ = ["LocalWorkspaceBackend"]
