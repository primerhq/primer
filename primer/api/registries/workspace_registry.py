"""Workspace backend registry — caches one backend per ``WorkspaceProvider`` row.

Mirrors the :class:`ProviderRegistry` pattern: lazy lookup, in-memory
cache keyed by row id, ``invalidate`` after row mutation, ``aclose``
on shutdown.

Adds two convenience helpers on top:

* :meth:`get_workspace` — given a ``Workspace`` row, finds the right
  backend (via the row's ``provider_id``) and asks the backend for the
  live :class:`Workspace` handle. Raises ``NotFoundError`` if the
  backend has never instantiated this id (e.g. after process restart
  with an ephemeral backend).
* :meth:`destroy` — same lookup, then ``backend.destroy(...)`` + row
  delete.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from primer.bootstrap.prime import resolve_default_workspace_id
from primer.model.common import dump_for_storage
from primer.model.except_ import ConflictError, NotFoundError, ValidationError
from primer.model.workspace_refusal import ReservedWorkspaceError, WorkspaceNotImplementedError, WorkspaceRefusedError
from primer.model.workspace import WORKSPACE_ID_PATTERN, Workspace as WorkspaceRow
from primer.model.workspace import (
    WorkspaceChannelLink,
    WorkspaceProvider,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.storage import raw_generation
from primer.workspace.env_names import refuse_bad_env_names
from primer.workspace.files import FileResolvers
from primer.workspace.local_policy import LocalWorkspacePolicy
from primer.workspace.resolvers import (
    make_document_resolver,
    make_secret_resolver,
)
from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost
from primer.workspace.size_overrides import (
    asks_for_a_size,
    refuse_oversized_template_sizes,
    refuse_unsupported_resource_overrides,
)


if TYPE_CHECKING:
    from primer.int.secret_provider import SecretProvider
    from primer.int.storage_provider import StorageProvider
    from primer.int.workspace import Workspace, WorkspaceBackend


logger = logging.getLogger(__name__)

#: How many times :meth:`WorkspaceRegistry._mark_terminating` reads the phase again when it moved under the fenced mark.
_MARK_TRIES = 3


#: The phase every create door writes a new workspace row with (ticket 01a16d72, ruling 2): ``pending`` until the probe's first good ping
#: promotes it to ``running`` (:mod:`primer.workspace.probe`). ``POST /v1/workspaces`` and the bootstrap seed used to write ``running`` and the
#: ``create_workspace`` tool ``pending``, so the same workspace read differently depending on who made it. No reader depends on ``running`` at
#: create: nothing gates a session start, a wake or a turn on ``pending`` versus ``running``, and the console reads the phase only for its
#: ``failed`` banner.
NEW_WORKSPACE_PHASE: Literal["pending"] = "pending"


def new_workspace_row(
    *,
    workspace_id: str,
    template: WorkspaceTemplate,
    overrides: WorkspaceTemplateOverrides | None,
    live: "Workspace",
    name: str | None = None,
    reply_binding: WorkspaceChannelLink | None = None,
) -> WorkspaceRow:
    """The durable row of a workspace ``materialise`` has just built, as every create door writes it (REST, the ``create_workspace`` tool, the
    bootstrap seed): the full create-time template snapshot (what re-attach uses), the provider the template names, the live instance's runtime
    coordinates, and :data:`NEW_WORKSPACE_PHASE`."""
    return WorkspaceRow(
        id=workspace_id,
        name=name,
        template_id=template.id,
        template_snapshot=template,
        provider_id=template.provider_id,
        overrides=overrides,
        created_at=datetime.now(timezone.utc),
        phase=NEW_WORKSPACE_PHASE,
        runtime_meta=live.runtime_meta,
        reply_binding=reply_binding,
    )


def _make_default_factory(
    subprocess_timeout_seconds: float = 120.0,
) -> "Callable[[WorkspaceProvider], WorkspaceBackend]":
    """Return a backend factory that threads ``subprocess_timeout_seconds`` through."""
    def _factory(provider: WorkspaceProvider) -> "WorkspaceBackend":  # pragma: no cover
        from primer.workspace.factory import WorkspaceBackendFactory
        return WorkspaceBackendFactory.create(
            provider,
            subprocess_timeout_seconds=subprocess_timeout_seconds,
        )
    return _factory


# Back-compat shim: kept so existing test code that imports _default_factory
# or passes factory=None still works (zero timeout argument -> default 120s).
def _default_factory(
    provider: WorkspaceProvider,
) -> "WorkspaceBackend":  # pragma: no cover
    """Production dispatch via :class:`WorkspaceBackendFactory`.

    Local import keeps the workspace runtime out of the hot import
    graph until a backend is actually constructed.
    """
    from primer.workspace.factory import WorkspaceBackendFactory

    return WorkspaceBackendFactory.create(provider)


class WorkspaceRegistry:
    """Lazy backend cache keyed by :class:`WorkspaceProvider` row id."""

    def __init__(
        self,
        storage_provider: "StorageProvider",
        *,
        factory: Callable[[WorkspaceProvider], "WorkspaceBackend"] | None = None,
        subprocess_timeout_seconds: float = 120.0,
        secret_provider: "SecretProvider | None" = None,
        local_policy: LocalWorkspacePolicy | None = None,
    ) -> None:
        self._sp = storage_provider
        self._secret_provider = secret_provider
        # Where a workspace on a local provider may be used (ticket 01a1072f). The default is the
        # permissive policy, so a registry built without one behaves exactly as before.
        self._local_policy = local_policy or LocalWorkspacePolicy()
        # If a custom factory is supplied, use it as-is; otherwise build a
        # default factory that captures the configured subprocess timeout.
        self._factory = factory or _make_default_factory(subprocess_timeout_seconds)
        self._cache: dict[str, "WorkspaceBackend"] = {}
        self._lock = asyncio.Lock()
        # Workspace ids a destroy() is running for in this process (a count, so two overlapping destroys of one id hold the mark until both end).
        self._destroying: dict[str, int] = {}
        # Creates of one explicit workspace id in this process, run one at a time (round 2 of #917): a lock and how many creates hold or wait on it.
        self._creating: dict[str, tuple[asyncio.Lock, list[int]]] = {}

    # ---- backend cache -----------------------------------------------

    @property
    def local_policy(self) -> LocalWorkspacePolicy:
        """Where a workspace on a local provider may be used; the health route and the create paths read it."""
        return self._local_policy

    async def get_backend(self, provider_id: str) -> "WorkspaceBackend":
        """The backend for *provider_id*; the ONE funnel every workspace access passes through.

        Raises :class:`WorkspaceRefusedError` for a local provider the deployment refuses
        (:class:`LocalWorkspacePolicy`), BEFORE the backend is built, so a refused provider never creates its
        directories on this process's disk. The policy is fixed for the registry's life and only an allowed
        backend is ever cached, so a cache hit needs no check of its own.
        """
        async with self._lock:
            cached = self._cache.get(provider_id)
            if cached is not None:
                return cached
            row = await self._sp.get_storage(WorkspaceProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"WorkspaceProvider {provider_id!r} does not exist"
                )
            self._local_policy.check_provider(row)
            backend = self._factory(row)
            await backend.initialize()
            self._cache[provider_id] = backend
            return backend

    async def check_workspace_allowed(self, workspace_id: str) -> None:
        """Raise :class:`WorkspaceRefusedError` when *workspace_id* sits on a provider this deployment refuses.

        A policy-only look (the workspace row and its provider row, no backend built, nothing attached), for a caller
        that must know BEFORE it reports or clears anything that depends on the workspace being usable. Costs nothing
        while the policy refuses nothing, which is the default.
        """
        if not self._local_policy.refuses_local:
            return
        row = await self.get_workspace_row(workspace_id)
        provider = await self._sp.get_storage(WorkspaceProvider).get(row.provider_id)
        if provider is None:
            return
        try:
            self._local_policy.check_provider(provider)
        except WorkspaceRefusedError as exc:
            raise exc.for_workspace(workspace_id) from None

    async def invalidate(self, provider_id: str) -> None:
        async with self._lock:
            backend = self._cache.pop(provider_id, None)
        if backend is not None:
            await backend.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            backends = list(self._cache.values())
            self._cache.clear()
        for backend in backends:
            try:
                await backend.aclose()
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "WorkspaceRegistry: aclose failed on %s: %s",
                    type(backend).__name__,
                    exc,
                )

    # ---- per-workspace helpers ---------------------------------------

    def is_being_destroyed(self, workspace_id: str) -> bool:
        """Whether a :meth:`destroy` of *workspace_id* is running in this process.

        :meth:`destroy` takes the runtime down first and deletes the row last, so for that long the row says the workspace exists although nothing
        is left of it. A caller whose write to the workspace failed (a steer that raced the delete) asks here, because the row alone would tell it the
        workspace is fine. Process-local: a destroy running in another process is not seen, and its row delete is what shows it.
        """
        return self._destroying.get(workspace_id, 0) > 0

    async def get_workspace_row(self, workspace_id: str) -> WorkspaceRow:
        """Fetch the persisted ``Workspace`` row; 404 if missing."""
        row = await self._sp.get_storage(WorkspaceRow).get(workspace_id)
        if row is None:
            raise NotFoundError(
                f"Workspace {workspace_id!r} does not exist"
            )
        return row

    async def get_workspace(self, workspace_id: str) -> "Workspace":
        """Resolve the live :class:`Workspace` handle.

        After a process restart the backend may have an empty in-memory
        cache. We pass the workspace's create-time template snapshot to
        ``backend.get`` so every backend can re-attach without consulting
        a template row that may since have changed or been deleted.

        A row with NO snapshot re-attaches from its template row and gets
        the snapshot written (:meth:`_backfill_template_snapshot`).
        """
        row = await self.get_workspace_row(workspace_id)
        try:
            backend = await self.get_backend(row.provider_id)
        except WorkspaceRefusedError as exc:
            # The gate only sees a provider; say which workspace the caller was resolving.
            raise exc.for_workspace(workspace_id) from None
        template = row.template_snapshot
        if template is None:
            template = await self._backfill_template_snapshot(row)
        ws = await backend.get(workspace_id, template=template)
        if ws is None:
            raise NotFoundError(
                f"Workspace {workspace_id!r} row exists but the backend "
                "has no live instance and re-attach failed — the "
                "underlying container/pod may have been destroyed "
                "out-of-band."
            )
        return ws

    async def _backfill_template_snapshot(self, row: WorkspaceRow) -> WorkspaceTemplate | None:
        """The template a row without a snapshot re-attaches with, written to the row as its snapshot.

        Migration 12 snapshots every row once, at boot, but a process still on the code before it (a worker running ``create_workspace``
        for an agent, a second API replica, during a rollout) goes on writing rows without one, and nothing backfilled those: every
        backend's re-attach got ``template=None`` and the workspace was "re-attach failed" for good although its template still
        existed (the G5 audit, G5-08). Such a row now re-attaches from its template row, as m012 would have snapshotted it, and the
        snapshot is written the way m012 writes it: a field-scoped ``patch_if`` of ``template_snapshot`` alone, guarded on the
        ``template_id`` read and an absent snapshot, so a concurrent writer's other fields survive and a snapshot written meanwhile
        wins (and is the one used). Best effort: a failed write is logged and the re-attach goes on with the template row. A row
        whose template is gone too returns ``None``, as before.
        """
        template = await self._sp.get_storage(WorkspaceTemplate).get(row.template_id)
        if template is None:
            logger.warning(
                "workspace %s has no template snapshot and its template %r no longer exists: it cannot be re-attached",
                row.id, row.template_id,
            )
            return None
        storage = self._sp.get_storage(WorkspaceRow)
        try:
            written = await storage.patch_if(
                row.id,
                {"template_snapshot": dump_for_storage(template)},
                where={"template_id": [raw_generation(row, "template_id")], "template_snapshot": [None]},
            )
            if written is None:
                written = await storage.get(row.id)      # a snapshot was written meanwhile: that one is the workspace's
        except Exception:  # noqa: BLE001 - best effort: the re-attach does not depend on the write
            logger.warning("workspace %s: writing the missing template snapshot failed", row.id, exc_info=True)
            return template
        if written is not None and written.template_snapshot is not None:
            return written.template_snapshot
        return template

    async def materialise(
        self,
        *,
        template: WorkspaceTemplate,
        overrides: WorkspaceTemplateOverrides | None = None,
        workspace_id: str | None = None,
    ) -> "Workspace":
        """Create a new live workspace via the right backend.

        ``workspace_id`` pins the live instance to a caller-supplied id
        (typically the same id the durable row is keyed by). Forwarding
        it keeps the row id and the live instance id in sync so re-attach
        via :meth:`get_workspace` works after the cache is evicted.

        A pinned id is validated here, at the ONE point every create path
        (REST, the ``create_workspace`` tool, the bootstrap seed) passes:
        it names a directory under the local workspace root, a container
        name and a k8s object name, so only a single DNS-1123 label is
        admitted (``WORKSPACE_ID_PATTERN``; #680 N7).

        So is the per-workspace size (platform revamp 2026-10, master Review
        Focus 5): a size the template's provider cannot honour (Local takes
        none, Docker has no disk quota) is a 422 naming
        ``overrides.resources.<field>`` (:mod:`primer.workspace.size_overrides`),
        raised BEFORE :meth:`get_backend`, so a refused create builds nothing
        and leaves nothing behind. The same door refuses the other inputs a
        cluster would reject late and as a 500 (lead ruling 17 of plan 08): an
        environment variable name that Kubernetes would refuse (not its
        ``^[-._a-zA-Z][-._a-zA-Z0-9]*$`` pattern, ``.``, ``..`` or a name
        starting with ``..``, or over 256 characters; so ``my-var`` and
        ``a.b`` pass) or that is one the platform sets itself
        (:mod:`primer.workspace.env_names`), a template CPU or memory
        above the sane maxima or not more than zero, and a Kubernetes
        template disk (``backend.pvc_size``) that is not a quantity of at
        most 64 TiB (a row stored before the model refused these still
        loads, so this is where it is caught).

        A Kubernetes provider with ``variant: agent_sandbox`` is refused here too
        (:class:`~primer.model.workspace_refusal.WorkspaceNotImplementedError`,
        ticket 01a16d72 ruling 1): the variant is accepted when the provider is
        created, but no backend builds it, and the Kubernetes backend would
        otherwise build the ``system`` StatefulSet on it. It used to be refused
        by the REST route alone, so the ``create_workspace`` tool built one.
        """
        if workspace_id is not None and re.fullmatch(
            WORKSPACE_ID_PATTERN, workspace_id
        ) is None:
            raise ValidationError(
                f"workspace id {workspace_id!r} is not a valid workspace id: "
                "expected one DNS-1123 label (lowercase letters, digits and "
                f"'-', at most 63 characters) matching {WORKSPACE_ID_PATTERN} "
                "(it names a directory under the workspace root)"
            )
        # The provider's own refusal first, before any input of the request is judged: nothing on this provider is built, whatever the request.
        # A provider that does not exist is get_backend's 404, not these checks'.
        provider = await self._sp.get_storage(WorkspaceProvider).get(template.provider_id)
        if provider is not None and provider.config.kind == "kubernetes" and getattr(provider.config, "variant", "system") == "agent_sandbox":
            raise WorkspaceNotImplementedError(provider.id)
        # The request's own inputs, all before any backend runs (lead ruling 17): an environment variable name the cluster would
        # refuse or the platform sets itself, and a size from the template above the sane maxima. The override sizes carry their
        # maxima in the model.
        refuse_bad_env_names(template, overrides)
        refuse_oversized_template_sizes(template, overrides)
        if provider is not None and asks_for_a_size(overrides):
            refuse_unsupported_resource_overrides(provider.config.kind, overrides)
        backend = await self.get_backend(template.provider_id)
        resolvers = FileResolvers(
            document_resolver=make_document_resolver(self._sp),
            secret_resolver=(
                make_secret_resolver(self._secret_provider)
                if self._secret_provider is not None
                else None
            ),
        )
        return await backend.create(
            template,
            overrides=overrides,
            workspace_id=workspace_id,
            resolvers=resolvers,
        )

    async def create(
        self,
        *,
        template: WorkspaceTemplate,
        overrides: WorkspaceTemplateOverrides | None = None,
        workspace_id: str | None = None,
        name: str | None = None,
        reply_binding: WorkspaceChannelLink | None = None,
    ) -> WorkspaceRow:
        """Create a workspace: the live instance, then its durable row. The one create path of ``POST /v1/workspaces`` and the ``create_workspace``
        tool (ticket 01a16d72): every refusal of :meth:`materialise` applies to both, and both write the same row (:func:`new_workspace_row`, so the
        same first phase, :data:`NEW_WORKSPACE_PHASE`).

        The row is written LAST, so a failure or a cancel after :meth:`materialise` would orphan the live instance: the probe loop is row-driven
        and nothing sweeps orphans (arch review D-I1). So everything after it tears the instance back down on any failure, ``BaseException``
        included (a client disconnect or a hard Cancel of a turn is the likeliest way in, and ``asyncio.CancelledError`` is not an
        ``Exception``), and re-raises the original error unchanged. :meth:`destroy` is row-driven and the row does not exist, so the rollback
        goes to the backend directly, best effort: a failed rollback is logged and never masks the original error or the cancel.

        What the callers check first stays theirs (the template lookup, an id already in use, the admin-only overrides, served masks, a
        session's reach), and so does what REST does after (seeding the ``main`` session).

        Two creates of one explicit id (round 2 of #917, item 10): both can pass a door's "is the id free" check, and a local backend ADOPTS an
        existing workspace directory, so the loser materialised over the winner's files and its rollback then deleted them under the
        winner's live row. Creates of one id in this process now run one at a time, and the second is refused with :class:`ConflictError`
        (the doors' words) before anything is materialised. A conflict at the row write itself (a create in another process won the row
        first) is never rolled back when the id was given: the instance is the winner's. Without an id the platform picked a fresh one, so
        the instance is this create's own and is torn down as before.
        """
        if workspace_id is None:
            return await self._create(template=template, overrides=overrides, workspace_id=None, name=name, reply_binding=reply_binding)
        lock, holders = self._creating.setdefault(workspace_id, (asyncio.Lock(), [0]))
        holders[0] += 1
        try:
            async with lock:
                if await self._sp.get_storage(WorkspaceRow).get(workspace_id) is not None:
                    raise ConflictError(f"Workspace with id {workspace_id!r} already exists")
                return await self._create(
                    template=template, overrides=overrides, workspace_id=workspace_id, name=name, reply_binding=reply_binding,
                )
        finally:
            holders[0] -= 1
            if holders[0] == 0:
                self._creating.pop(workspace_id, None)

    async def _create(
        self,
        *,
        template: WorkspaceTemplate,
        overrides: WorkspaceTemplateOverrides | None,
        workspace_id: str | None,
        name: str | None,
        reply_binding: WorkspaceChannelLink | None,
    ) -> WorkspaceRow:
        """:meth:`create`'s body: materialise, then the row, with the rollback."""
        live = await self.materialise(template=template, overrides=overrides, workspace_id=workspace_id)
        try:
            row = new_workspace_row(
                workspace_id=workspace_id if workspace_id is not None else live.id,
                template=template,
                overrides=overrides,
                live=live,
                name=name,
                reply_binding=reply_binding,
            )
            await self._sp.get_storage(WorkspaceRow).create(row)
        except BaseException as exc:
            if workspace_id is not None and isinstance(exc, ConflictError):
                # Another create of this id won the row (in another process: this one runs them one at a time). The live instance is the
                # winner's (a local backend adopted its directory), so it is never torn down here.
                logger.warning("create_workspace: workspace %s was created by another caller first; its instance is left alone", workspace_id)
                raise
            try:
                backend = await self.get_backend(template.provider_id)
                await backend.destroy(live.id)
            except Exception:
                logger.exception("create_workspace: failed to roll back orphaned live workspace %s after a post-materialise error", live.id)
            raise
        return row

    async def destroy(self, workspace_id: str, *, before_teardown: Callable[[], Awaitable[None]] | None = None) -> None:
        """Destroy the live workspace AND drop the persisted row.

        Refuses the default workspace (prime) with :class:`ReservedWorkspaceError`, as the FIRST thing it does: before the row is
        read, before any backend is built or called, before a session is touched. This is the one funnel every workspace delete passes
        (the REST route and the ``delete_workspace`` tool both end here), so the rule lives here and the layers above only translate
        the error. The default is whatever :func:`~primer.bootstrap.prime.resolve_default_workspace_id` names, so it is the recorded id,
        or, on an install that predates the record, the legacy ``primer`` row.

        Reconciles open sessions to ENDED/``workspace_lost`` BEFORE the
        row is deleted (real bug fixed here: the row used to be
        hard-deleted first, but the probe's own ``workspace_lost``
        reconciliation only runs as a side effect of observing a row
        transition to ``phase == "failed"`` -- with the row already
        gone, that transition can never be observed, so sessions in a
        destroyed workspace were silently orphaned instead of ending).

        The order is: tear the runtime down (``backend.destroy``), end every
        open session, delete the row. The session read pages through ALL of
        the workspace's open sessions (ticket 01a11b93), and it is
        best-effort: if the read fails, the destroy does NOT refuse and does
        NOT retry. The runtime is already gone when it runs, so refusing
        would leave a row without a runtime, a retry inside the request
        cannot make a failing query work, and the probe cannot end the
        sessions later (the row is deleted). The failure is logged ("session
        reconcile: failed to query sessions") and the destroy goes on.

        From the first step to the row delete :meth:`is_being_destroyed` is true for the id, so a caller whose write to the workspace failed under
        a racing destroy can tell that from a fault (the session wake does), and a session start refuses the workspace
        (:func:`primer.workspace.session_factory.start_workspace_session`).

        ``before_teardown`` (review of #874, D1) runs once the workspace is marked as being destroyed and before anything is torn down: the
        rank scan of a delete (every open session in the workspace must be one the caller may act on) runs here, so a session started from now on
        is refused instead of being ended unranked. If it raises, nothing is torn down and the error propagates. The fence is process-local
        (like :meth:`is_being_destroyed`), and a start already past its check when the mark is set can still write its row after the scan.

        Once ``before_teardown`` has passed, and before the backend is reached, the row's ``phase`` is written ``terminating`` (ticket 01a16d72
        ruling 3; nothing wrote it before), so a reader sees the delete in progress: the probe skips the row and the reply-binding route refuses
        it. A refused delete (the default workspace, an unknown id, the rank scan) never writes it. If the destroy then fails,
        the row must not stay ``terminating``, which nothing would ever move again (:meth:`_after_a_failed_destroy`).
        """
        if workspace_id == await resolve_default_workspace_id(self._sp):
            raise ReservedWorkspaceError(workspace_id)
        row = await self.get_workspace_row(workspace_id)
        # From here to the row delete the runtime may be gone while the row still says the workspace exists: see is_being_destroyed.
        self._destroying[workspace_id] = self._destroying.get(workspace_id, 0) + 1
        try:
            if before_teardown is not None:
                await before_teardown()
            marked_from = await self._mark_terminating(workspace_id)
            teardown_started = False
            try:
                try:
                    backend = await self.get_backend(row.provider_id)
                except WorkspaceRefusedError:
                    # An explicit destroy is how an operator abandons a workspace this deployment refuses
                    # (ticket 01a1072f). The backend is not reachable by policy, so only the row goes (and its
                    # sessions, below): whatever the workspace left on the provider's disk stays for the operator.
                    logger.warning(
                        "WorkspaceRegistry: destroying workspace %s on the refused local provider %s drops "
                        "its row only; its files are left in place",
                        workspace_id, row.provider_id,
                    )
                    backend = None
                teardown_started = True
                try:
                    if backend is not None:
                        await backend.destroy(workspace_id)
                except NotFoundError:
                    # Row exists but backend has no live instance; still drop
                    # the row so callers can re-materialise without a stale
                    # collision.
                    pass

                await reconcile_sessions_to_workspace_lost(self._sp, workspace_id)
                await self._sp.get_storage(WorkspaceRow).delete(workspace_id)
            except BaseException as exc:
                # BaseException: a cancel (a client disconnect on REST) leaves the teardown as unfinished as an error does.
                await self._after_a_failed_destroy(workspace_id, exc, teardown_started=teardown_started, marked_from=marked_from)
                raise
        finally:
            left = self._destroying[workspace_id] - 1
            if left > 0:
                self._destroying[workspace_id] = left
            else:
                del self._destroying[workspace_id]

    async def _mark_terminating(self, workspace_id: str) -> str | None:
        """Write ``phase = terminating`` and return the phase it replaced, or ``None`` when the row was already ``terminating`` (a destroy
        of the same id in another process: its mark is left as it is, and this destroy puts nothing back).

        One field-scoped write fenced on the phase just read, so a concurrent writer's other fields (a rename, the probe's ``last_probe_at``)
        survive and the phase a failed destroy may put back is the one the mark replaced, not the one :meth:`destroy` read before its rank
        scan (the probe may have failed the workspace in between; round 2 of #917, item 8). A phase that moved between the read and the
        write is read again; one that keeps moving (only the probe moves it, every ~30 s) is marked whatever it is after a few tries.
        """
        storage = self._sp.get_storage(WorkspaceRow)
        for _ in range(_MARK_TRIES):
            current = await self.get_workspace_row(workspace_id)
            if current.phase == "terminating":
                return None
            written = await storage.patch_if(
                workspace_id, {"phase": "terminating"}, where={"phase": [raw_generation(current, "phase")]},
            )
            if written is not None:
                return current.phase
        written = await storage.patch_if(workspace_id, {"phase": "terminating"}, where={"phase": ["pending", "running", "failed"]})
        return current.phase if written is not None else None

    async def _after_a_failed_destroy(
        self, workspace_id: str, exc: BaseException, *, teardown_started: bool, marked_from: str | None,
    ) -> None:
        """Leave a phase that tells the truth after a destroy failed (or was cancelled) past its ``terminating`` mark.

        * Nothing was torn down because the backend could not be RESOLVED (:meth:`get_backend` raised: no provider row, or its backend could
          not be built or initialised): the workspace is as it was, so the phase the mark replaced goes back and its sessions are left alone.
        * The teardown itself failed, or the row could not be deleted after it: the runtime may be partly or wholly gone. An outage during the
          teardown call is this case too (a Docker daemon or a cluster API that is down, with the backend already resolved and cached, which
          it is once the probe or a create has used it): nothing may have been torn down, but the destroy cannot tell. The row reads
          ``failed``, with a ``failure_reason`` that says the delete did not finish and why, and every open session is ended
          ``workspace_lost``, as on every other transition to ``failed`` (the probe's): a failed workspace has ended its open sessions. The
          probe brings an intact workspace back to ``running`` after three good probes, and a ``workspace_lost`` session reopens then
          (:func:`primer.session.reset.refuse_a_reopen_it_may_not_make`). Asking for the delete again finishes it.

        Both writes are fenced on the row still reading ``terminating`` (this destroy's own mark), and best effort: the database may be what
        failed. A write that fails is logged and leaves the row ``terminating``; the original error is never masked.
        """
        storage = self._sp.get_storage(WorkspaceRow)
        try:
            if not teardown_started:
                if marked_from is not None:
                    await storage.patch_if(workspace_id, {"phase": marked_from}, where={"phase": ["terminating"]})
                return
            cause = " ".join(str(exc).split()) or type(exc).__name__
            reason = f"the delete did not finish ({type(exc).__name__}: {cause[:300]}); the runtime may be partly torn down: delete it again"
            written = await storage.patch_if(
                workspace_id, {"phase": "failed", "failure_reason": reason}, where={"phase": ["terminating"]},
            )
            if written is not None:
                await reconcile_sessions_to_workspace_lost(self._sp, workspace_id)
        except NotFoundError:
            return      # the row is gone after all: nothing is left to describe
        except Exception:  # noqa: BLE001 - best effort, never masks the destroy's own error
            logger.warning(
                "WorkspaceRegistry: destroy of %s failed and its phase could not be corrected; the row stays terminating",
                workspace_id, exc_info=True,
            )


__all__ = ["NEW_WORKSPACE_PHASE", "WorkspaceRegistry", "new_workspace_row"]
