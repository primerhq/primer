"""BaseWorkspaceBackend -- shared cache/lock lifecycle + merge/materialize.

The three concrete backends (local FS, container, kubernetes) all share
the same in-memory ``workspace_id -> Workspace`` cache guarded by an
``asyncio.Lock``, the same template/override merge semantics, and the
same "resolve every FileSource then write the bytes" loop. This base
class hosts that shared scaffolding so each subclass only carries its
backend-specific materialisation (local fs writes, container volume,
k8s Secret/Service/StatefulSet/HTTPRoute) and its re-attach hook.

Crucially, :meth:`get` evicts a cached handle whose runtime client has
gone ``gone`` (the runtime self-evicts on a 404 handshake; see
:attr:`primer.workspace.runtime.runtime_client.RuntimeClient.gone`).
Without the eviction the cache would keep handing out a dead handle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from primer.int.workspace import Workspace, WorkspaceBackend
from primer.workspace.files import (
    FileResolvers,
    ResolvedFile,
    resolve_file_sources,
)

if TYPE_CHECKING:
    from pydantic import SecretStr

    from primer.model.workspace import (
        FileMount,
        WorkspaceTemplate,
        WorkspaceTemplateOverrides,
    )


logger = logging.getLogger(__name__)


class MergedTemplate:
    """The result of merging a template with its per-instantiation overrides.

    ``env`` keeps the :class:`pydantic.SecretStr` wrappers (use
    :meth:`env_unwrapped` to get a plain ``dict[str, str]`` for a real
    process / container env). ``files`` and ``init_commands`` follow the
    merge-then-extend rule: template entries first, override entries
    appended.
    """

    __slots__ = ("env", "files", "init_commands")

    def __init__(
        self,
        *,
        env: "dict[str, SecretStr]",
        files: "list[FileMount]",
        init_commands: list[str],
    ) -> None:
        self.env = env
        self.files = files
        self.init_commands = init_commands

    def env_unwrapped(self) -> dict[str, str]:
        """Unwrap every :class:`SecretStr` for use as a real env mapping."""
        return {k: v.get_secret_value() for k, v in self.env.items()}


class BaseWorkspaceBackend(WorkspaceBackend):
    """Shared cache/lock lifecycle for the concrete workspace backends.

    Subclasses MUST:

    * call ``super().__init__()`` to set up ``_workspaces`` / ``_lock`` /
      ``_initialised``;
    * implement :meth:`_reattach` (rebuild a handle for a workspace this
      process didn't materialise, or return ``None``);
    * use :meth:`merge_overrides` + :meth:`materialize_files_on_backend`
      in their ``create`` flow;
    * register the freshly-created handle via the ``_workspaces`` dict
      under ``_lock`` (or :meth:`_register`).
    """

    def __init__(self) -> None:
        # ``Workspace`` value type is intentionally broad here; each
        # subclass narrows the concrete handle type in its own annotation.
        self._workspaces: dict[str, Workspace] = {}
        self._lock = asyncio.Lock()
        self._initialised = False

    # ---- template / override merge --------------------------------------

    @staticmethod
    def merge_overrides(
        template: "WorkspaceTemplate",
        overrides: "WorkspaceTemplateOverrides | None",
    ) -> MergedTemplate:
        """Merge ``overrides`` onto ``template`` (merge-then-extend).

        ``env`` overlays (override keys win); ``files`` and
        ``init_commands`` extend (template entries first). Returns a
        :class:`MergedTemplate`; the SecretStr env wrappers are preserved
        so callers decide when to unwrap.
        """
        merged_env = dict(template.env)
        files = list(template.files)
        init_commands = list(template.init_commands)
        if overrides is not None:
            merged_env.update(overrides.env)
            files = files + list(overrides.files)
            init_commands = init_commands + list(overrides.init_commands)
        return MergedTemplate(
            env=merged_env, files=files, init_commands=init_commands,
        )

    # ---- file materialisation -------------------------------------------

    @staticmethod
    async def materialize_files_on_backend(
        files: "list[FileMount]",
        writer: Callable[[ResolvedFile], Awaitable[None]],
        *,
        resolvers: FileResolvers | None,
    ) -> None:
        """Resolve every FileSource variant then write each via ``writer``.

        The document/secret resolvers are supplied by the orchestration
        layer (``WorkspaceRegistry.materialise``) via the ``resolvers``
        bundle; when absent, those source kinds raise during resolution.
        Each backend supplies its own ``writer`` (local fs write, sandbox
        WS write, ...) so only the resolve loop is shared.
        """
        resolved_files = await resolve_file_sources(
            files,
            document_resolver=resolvers.document_resolver if resolvers else None,
            secret_resolver=resolvers.secret_resolver if resolvers else None,
        )
        for rf in resolved_files:
            await writer(rf)

    # ---- cache helpers --------------------------------------------------

    async def _register(self, workspace_id: str, ws: Workspace) -> None:
        """Insert ``ws`` into the cache under the lock."""
        async with self._lock:
            self._workspaces[workspace_id] = ws

    async def _cached_live(self, workspace_id: str) -> Workspace | None:
        """Return the cached handle for ``workspace_id`` if it is still live.

        Evicts (and returns ``None`` for) a handle whose runtime client has
        gone ``gone`` -- the runtime self-evicts on a 404 handshake but the
        cache would otherwise keep handing out the dead handle. The gone client
        has already closed its own WS + aiohttp session when it gave up; what
        is left to do is the handle's ``aclose``, which ends its live sessions
        over that dead connection (each fails at once, as a closed client
        refuses a request), run through ``close_shielded`` so a failure is
        logged and the wait is bounded.
        """
        async with self._lock:
            cached = self._workspaces.get(workspace_id)
            if cached is None:
                return None
            if not cached.gone:
                return cached
            # Dead handle: drop it so we fall through to re-attach.
            self._workspaces.pop(workspace_id, None)
        logger.info(
            "%s: cached workspace %s is gone; evicting and re-attaching",
            type(self).__name__, workspace_id,
        )
        # ``aclose`` ends the handle's live sessions, each a state commit over the very connection that is gone, and a request
        # on a disconnected ``RuntimeClient`` waits for a reconnect that never comes: no bound of its own, and ``get`` is on
        # the caller's path. ``close_shielded`` bounds it and logs a failure.
        await close_shielded(
            cached, what=f"{type(self).__name__}: evicted gone workspace {workspace_id}",
        )
        return None

    # ---- get (template method) ------------------------------------------

    async def get(
        self,
        workspace_id: str,
        *,
        template: "WorkspaceTemplate | None" = None,
    ) -> Workspace | None:
        """Return a live handle for ``workspace_id``, or ``None``.

        Returns the cached handle when one is live; evicts it first when
        its runtime client has gone ``gone`` (fix for the dead-handle
        cache bug). On a cache miss (or after eviction) delegates to the
        subclass :meth:`_reattach` to rebuild the handle from durable
        backend state, which returns ``None`` when re-attach is impossible
        (no ``template``, no backing object, ...).
        """
        cached = await self._cached_live(workspace_id)
        if cached is not None:
            return cached
        return await self._reattach(workspace_id, template)

    async def _reattach(
        self,
        workspace_id: str,
        template: "WorkspaceTemplate | None",
    ) -> Workspace | None:
        """Rebuild a handle for a workspace this process didn't materialise.

        Subclasses implement the backend-specific re-attach (local: rebuild
        from the on-disk dir; container: adapter.get_sandbox; k8s: read the
        StatefulSet + recover the token). MUST return ``None`` when no
        backing object exists or no ``template`` was supplied. Implementations
        own their own post-build race re-check against ``_workspaces``.
        """
        raise NotImplementedError


#: How long a caller that must release a connection waits for the close before it carries on and leaves the close running.
#: A peer that has gone silent can keep ``aclose`` (a WebSocket close handshake, an aiohttp session close) waiting for its
#: own timeouts, and the caller is often under a bound of its own (the relay's read: 5 seconds).
_CLOSE_WAIT_S = 3.0

#: The closes in flight, held so a close that outlives its caller (a second cancel, the bound above) is not garbage collected
#: before it has finished.
_PENDING_CLOSES: "set[asyncio.Future]" = set()


def _close_finished(task: "asyncio.Future", what: str) -> None:
    _PENDING_CLOSES.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("%s: aclose failed: %s", what, task.exception())


async def close_shielded(closable: object, *, what: str) -> None:
    """Close what a build that did not finish left open: a ``RuntimeClient``, a ``WSSandbox``, anything with ``aclose``.

    Run from the ``except BaseException`` (or ``finally``) of a backend's create or re-attach, so a cancel or a caller's
    ``asyncio.timeout`` that ended the build is covered too. The close runs on its OWN task, so a second cancel (a drain, a
    bound landing again) while the caller waits for it cannot leave the socket and the aiohttp session half released, and
    the caller's wait is BOUNDED (``_CLOSE_WAIT_S``): past it the close carries on in the background and the caller goes on
    with the error that ended the build, which a silent peer would otherwise have stretched by the close's own timeouts. A
    close that fails is logged and never replaces that error.
    """
    aclose = getattr(closable, "aclose", None)
    if aclose is None:
        return
    closing = asyncio.ensure_future(aclose())
    _PENDING_CLOSES.add(closing)
    closing.add_done_callback(lambda task: _close_finished(task, what))
    # ``asyncio.wait`` does not cancel what it waits on, so a cancel of the caller leaves the close running.
    done, _ = await asyncio.wait({closing}, timeout=_CLOSE_WAIT_S)
    if not done:
        logger.warning(
            "%s: aclose still running after %gs; carrying on without waiting for it", what, _CLOSE_WAIT_S,
        )


__all__ = ["BaseWorkspaceBackend", "MergedTemplate", "close_shielded"]
