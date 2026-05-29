"""BootstrapRunner — idempotent first-boot provider creation.

Creates the four reserved-id providers on first boot so a fresh
``primer api`` install is immediately usable. Each ``_ensure_*``
method is independent: a failure in one records the error and
continues so the other providers are still attempted.

The bootstrap marker (``system_state.bootstrap_completed_at``) is only
stamped when ALL four ensure-steps succeed. If any step errored the
marker stays NULL so a subsequent run (or ``primer init --force``) can
retry.

Usage example (from the lifespan)::

    runner = BootstrapRunner(
        storage=storage_provider,
        embedder_storage=storage_provider.get_storage(EmbeddingProvider),
        ssp_storage=storage_provider.get_storage(SemanticSearchProvider),
        cross_encoder_storage=storage_provider.get_storage(CrossEncoderProvider),
        workspace_provider_storage=storage_provider.get_storage(WorkspaceProvider),
        root_dir=Path("~/.primer").expanduser(),
    )
    if await runner.needs_bootstrap():
        result = await runner.run()
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from primer.bootstrap.defaults import (
    RESERVED_CROSS_ENCODERS,
    RESERVED_EMBEDDERS,
    RESERVED_HUGGINGFACE_CROSS_ENCODER,
    RESERVED_HUGGINGFACE_EMBEDDER,
    RESERVED_LANCE_SSP,
    RESERVED_LOCAL_WORKSPACE_PROVIDER,
    RESERVED_SSPS,
    RESERVED_WORKSPACE_PROVIDERS,
)
from primer.model.provider import (
    CrossEncoderProvider,
    EmbeddingProvider,
    SemanticSearchProvider,
)
from primer.model.workspace import WorkspaceProvider

if TYPE_CHECKING:
    from primer.int.storage import Storage
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


@dataclass
class BootstrapResult:
    """Outcome of a single :meth:`BootstrapRunner.run` call.

    Attributes
    ----------
    created:
        Reserved ids that were absent and were created successfully.
    skipped:
        Reserved ids that were already present and were left unchanged.
    errors:
        ``(reserved_id, repr(exception))`` pairs for any step that raised.
        If this list is non-empty the bootstrap marker is NOT stamped,
        and a subsequent run will retry the failed steps.
    """

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


class BootstrapRunner:
    """Idempotently creates the four reserved providers on first boot.

    Parameters
    ----------
    storage:
        The top-level :class:`~primer.int.storage_provider.StorageProvider`.
        Used for ``get_system_state`` / ``set_bootstrap_completed`` only.
    embedder_storage:
        :class:`~primer.int.storage.Storage` bound to
        :class:`~primer.model.provider.EmbeddingProvider`.
    ssp_storage:
        :class:`~primer.int.storage.Storage` bound to
        :class:`~primer.model.provider.SemanticSearchProvider`.
    cross_encoder_storage:
        :class:`~primer.int.storage.Storage` bound to
        :class:`~primer.model.provider.CrossEncoderProvider`.
    workspace_provider_storage:
        :class:`~primer.int.storage.Storage` bound to
        :class:`~primer.model.workspace.WorkspaceProvider`.
    root_dir:
        Filesystem root used for resolving tilde paths in the factory
        specs (e.g. ``~/.primer/workspaces`` → ``root_dir / "workspaces"``).
        The directory is NOT created here; providers create sub-dirs on
        first use.
    """

    def __init__(
        self,
        *,
        storage: "StorageProvider",
        embedder_storage: "Storage[EmbeddingProvider]",
        ssp_storage: "Storage[SemanticSearchProvider]",
        cross_encoder_storage: "Storage[CrossEncoderProvider]",
        workspace_provider_storage: "Storage[WorkspaceProvider]",
        root_dir: Path,
    ) -> None:
        self._storage = storage
        self._embedder_storage = embedder_storage
        self._ssp_storage = ssp_storage
        self._cross_encoder_storage = cross_encoder_storage
        self._wp_storage = workspace_provider_storage
        self._root_dir = root_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def needs_bootstrap(self) -> bool:
        """Return ``True`` iff ``system_state.bootstrap_completed_at IS NULL``."""
        state = await self._storage.get_system_state()
        return state.bootstrap_completed_at is None

    async def run(self, *, force: bool = False) -> BootstrapResult:
        """Idempotently create the four reserved providers.

        Parameters
        ----------
        force:
            When *True*, ignore the completion marker and re-attempt all
            ensure-steps.  Still skips rows that already exist.

        Returns
        -------
        BootstrapResult
            Contains the ids that were created, skipped, or errored.
        """
        if not force and not await self.needs_bootstrap():
            # Marker is set and caller did not ask for force — nothing to do.
            # Retrieve all four as skipped so callers see a consistent result.
            result = BootstrapResult()
            await self._skip_all_present(result)
            return result

        result = BootstrapResult()
        await self._ensure_local_workspace_provider(result)
        await self._ensure_huggingface_embedder(result)
        await self._ensure_lance_ssp(result)
        await self._ensure_huggingface_cross_encoder(result)

        if not result.errors:
            await self._storage.set_bootstrap_completed(datetime.now(UTC))
            logger.info(
                "bootstrap complete — created=%r skipped=%r",
                result.created,
                result.skipped,
            )
        else:
            logger.warning(
                "bootstrap finished with errors — marker NOT stamped; "
                "retry with 'primer init --force'. errors=%r",
                result.errors,
            )

        return result

    # ------------------------------------------------------------------
    # Private: ensure-* steps
    # ------------------------------------------------------------------

    async def _ensure_local_workspace_provider(
        self, result: BootstrapResult
    ) -> None:
        reserved_id = RESERVED_LOCAL_WORKSPACE_PROVIDER
        try:
            existing = await self._wp_storage.get(reserved_id)
            if existing is not None:
                result.skipped.append(reserved_id)
                return

            raw_spec = RESERVED_WORKSPACE_PROVIDERS[reserved_id]
            spec = self._resolve_wp_paths(copy.deepcopy(raw_spec))
            entity = WorkspaceProvider(**spec)
            await self._wp_storage.create(entity)
            result.created.append(reserved_id)
            logger.debug("bootstrap: created workspace provider %r", reserved_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "bootstrap: _ensure_local_workspace_provider failed: %s", exc
            )
            result.errors.append((reserved_id, repr(exc)))

    async def _ensure_huggingface_embedder(
        self, result: BootstrapResult
    ) -> None:
        reserved_id = RESERVED_HUGGINGFACE_EMBEDDER
        try:
            existing = await self._embedder_storage.get(reserved_id)
            if existing is not None:
                result.skipped.append(reserved_id)
                return

            spec = copy.deepcopy(RESERVED_EMBEDDERS[reserved_id])
            entity = EmbeddingProvider(**spec)
            await self._embedder_storage.create(entity)
            result.created.append(reserved_id)
            logger.debug("bootstrap: created embedder %r", reserved_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "bootstrap: _ensure_huggingface_embedder failed: %s", exc
            )
            result.errors.append((reserved_id, repr(exc)))

    async def _ensure_lance_ssp(
        self, result: BootstrapResult
    ) -> None:
        reserved_id = RESERVED_LANCE_SSP
        try:
            existing = await self._ssp_storage.get(reserved_id)
            if existing is not None:
                result.skipped.append(reserved_id)
                return

            raw_spec = RESERVED_SSPS[reserved_id]
            spec = self._resolve_ssp_paths(copy.deepcopy(raw_spec))
            entity = SemanticSearchProvider(**spec)
            await self._ssp_storage.create(entity)
            result.created.append(reserved_id)
            logger.debug("bootstrap: created SSP %r", reserved_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "bootstrap: _ensure_lance_ssp failed: %s", exc
            )
            result.errors.append((reserved_id, repr(exc)))

    async def _ensure_huggingface_cross_encoder(
        self, result: BootstrapResult
    ) -> None:
        reserved_id = RESERVED_HUGGINGFACE_CROSS_ENCODER
        try:
            existing = await self._cross_encoder_storage.get(reserved_id)
            if existing is not None:
                result.skipped.append(reserved_id)
                return

            spec = copy.deepcopy(RESERVED_CROSS_ENCODERS[reserved_id])
            entity = CrossEncoderProvider(**spec)
            await self._cross_encoder_storage.create(entity)
            result.created.append(reserved_id)
            logger.debug("bootstrap: created cross-encoder %r", reserved_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "bootstrap: _ensure_huggingface_cross_encoder failed: %s", exc
            )
            result.errors.append((reserved_id, repr(exc)))

    # ------------------------------------------------------------------
    # Private: path resolution helpers
    # ------------------------------------------------------------------

    def _resolve_wp_paths(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Replace tilde paths in the workspace-provider config dict.

        ``spec["config"]["root_path"]`` may be ``"~/.primer/workspaces"`` —
        replace the ``~/.primer`` prefix with ``self._root_dir``.
        """
        cfg = spec.get("config", {})
        if isinstance(cfg, dict) and "root_path" in cfg:
            cfg["root_path"] = str(self._resolve_path(str(cfg["root_path"])))
            spec["config"] = cfg
        return spec

    def _resolve_ssp_paths(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Replace tilde paths in the SSP config dict.

        ``spec["config"]["path"]`` may be ``"~/.primer/vector"``.
        """
        cfg = spec.get("config", {})
        if isinstance(cfg, dict) and "path" in cfg:
            cfg["path"] = self._resolve_path(str(cfg["path"]))
            spec["config"] = cfg
        return spec

    def _resolve_path(self, raw: str) -> Path:
        """Resolve a ``~/.primer/...`` template against ``root_dir``.

        If the path starts with ``~/.primer/`` the ``~/.primer`` prefix
        is replaced with ``self._root_dir``.  Any remaining ``~``-only
        path is expanded via :func:`Path.expanduser`.  Absolute or
        relative paths without ``~`` are returned unchanged.
        """
        _tilde_primer = "~/.primer"
        if raw.startswith(_tilde_primer):
            suffix = raw[len(_tilde_primer):]  # e.g. "/workspaces"
            return self._root_dir / suffix.lstrip("/")
        return Path(raw).expanduser()

    # ------------------------------------------------------------------
    # Private: helpers
    # ------------------------------------------------------------------

    async def _skip_all_present(self, result: BootstrapResult) -> None:
        """Fill ``result.skipped`` with all four reserved ids.

        Used when the marker is already set (force=False, early exit).
        We don't query storage — just mark all as skipped since by
        definition the marker only gets stamped when all four succeeded.
        """
        result.skipped.extend([
            RESERVED_LOCAL_WORKSPACE_PROVIDER,
            RESERVED_HUGGINGFACE_EMBEDDER,
            RESERVED_LANCE_SSP,
            RESERVED_HUGGINGFACE_CROSS_ENCODER,
        ])


__all__ = ["BootstrapResult", "BootstrapRunner"]
