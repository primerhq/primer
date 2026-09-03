"""Ordered, idempotent, backend-agnostic data migrations.

Primer's entity storage is schemaless JSONB with per-model tables created
lazily on first handle use, so there is no DDL to version. What does need
versioning is the SHAPE of the data inside those tables: renaming a field,
splitting one entity into two, moving a value from one row to another.

Three properties make this safe without a heavyweight migration framework:

* **Backend-agnostic.** Migrations operate through ``Storage[T]`` handles,
  never raw SQL, so Postgres and SQLite behave identically with no
  duplicated statements. The cost is paged reads plus per-row updates,
  which is irrelevant at the scale of providers, agents, and collections.
* **Idempotent.** Every migration is get-then-create, so a process death
  mid-run re-runs harmlessly on the next boot.
* **Forward-only.** ``schema_version`` is stamped after each migration
  commits, so a crash resumes from the last completed version. Downgrades
  are not attempted; see :func:`run_migrations`.

``SystemState.schema_version = N`` means migrations 1..N have been applied.
The field defaults to ``1``, which correctly describes every install that
predates this module (see :mod:`~primer.storage.migrations.m001_document_content`).

To add a migration: create ``mNNN_<slug>.py`` exposing a class satisfying
:class:`Migration`, then append an instance to :data:`MIGRATIONS`. The
contract test in ``tests/storage/test_migrations.py`` pins that versions
stay contiguous and unique.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from primer.int.storage_provider import StorageProvider
from primer.storage.migrations.m001_document_content import M001DocumentContent
from primer.storage.migrations.m002_model_profiles import M002ModelProfiles
from primer.storage.migrations.m003_session_cutover import M003SessionCutover
from primer.storage.migrations.m004_document_slugs import M004DocumentSlugs
from primer.storage.migrations.m005_document_directories import (
    M005DocumentDirectories,
)
from primer.storage.migrations.m006_unified_search_grants import (
    M006UnifiedSearchGrants,
)
from primer.storage.migrations.m007_aggregated_model_profiles import (
    M007AggregatedModelProfiles,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class Migration(Protocol):
    """One versioned, idempotent transformation of stored data."""

    version: int
    description: str

    async def apply(self, sp: StorageProvider) -> None:
        """Transform stored rows. Must be safe to run more than once."""
        ...


#: Ordered registry. Index position is not significant; ``version`` is.
MIGRATIONS: tuple[Migration, ...] = (
    M001DocumentContent(),
    M002ModelProfiles(),
    M003SessionCutover(),
    M004DocumentSlugs(),
    M005DocumentDirectories(),
    M006UnifiedSearchGrants(),
    M007AggregatedModelProfiles(),
)

#: Highest version this build knows how to apply.
LATEST_VERSION: int = len(MIGRATIONS)


async def run_migrations(
    sp: StorageProvider, *, is_fresh_install: bool,
) -> int:
    """Apply every migration newer than the stored ``schema_version``.

    A fresh install has no rows to transform, so it baselines straight to
    :data:`LATEST_VERSION` rather than running the chain against an empty
    database. ``is_fresh_install`` is decided by the caller from
    ``bootstrap_completed_at IS NULL``.

    When the database reports a version NEWER than this build understands,
    the runner logs a warning and returns without touching anything. Primer
    has no down-migration story, and guessing at one would be worse than
    refusing: the operator is running old code against a newer database and
    needs to know that, not have it silently half-handled.

    Returns the resulting schema version.
    """
    state = await sp.get_system_state()
    current = state.schema_version or 0

    if is_fresh_install:
        await sp.set_schema_version(LATEST_VERSION, migrated_at=datetime.now(UTC))
        logger.info(
            "fresh install; baselined schema version",
            extra={"schema_version": LATEST_VERSION},
        )
        return LATEST_VERSION

    if current > LATEST_VERSION:
        logger.warning(
            "database schema version is newer than this build; "
            "refusing to downgrade",
            extra={"db_version": current, "build_version": LATEST_VERSION},
        )
        return current

    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        logger.info(
            "applying migration",
            extra={
                "version": migration.version,
                "description": migration.description,
            },
        )
        await migration.apply(sp)
        await sp.set_schema_version(
            migration.version, migrated_at=datetime.now(UTC),
        )
        current = migration.version

    return current


__all__ = ["LATEST_VERSION", "MIGRATIONS", "Migration", "run_migrations"]
