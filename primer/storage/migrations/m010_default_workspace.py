"""Migration 10: record an existing install's default workspace and name it prime (pivot plan P2 task 5).

An install that predates ``SystemState.default_workspace_id`` keeps the id its default workspace has always had
(``primer``): the id is only recorded, never moved, because that workspace's sessions, files and Kubernetes objects
are keyed by the id. The display name is what changes: the row is renamed ``prime`` once, for display, and the id is
recorded, so from the next boot the default is named by ``SystemState.default_workspace_id`` and read with
``primer.bootstrap.prime.resolve_default_workspace_id``.

The migration delegates to ``adopt_legacy_default_workspace``, the SAME function the ensure pass
(``primer.bootstrap/seed.py::ensure_default_workspace``) calls. A database whose migrations were baselined
(``auto_bootstrap: false`` never stamped a bootstrap marker, so the runner treats it as fresh and runs no migration)
still reaches the rule through the pass, and the two must not drift apart; one function both call is the guarantee.
The function acts only while nothing is recorded and a ``primer`` row exists, and then once, so it is idempotent: on
a fresh install (no row, nothing recorded) it records nothing, and once an id is recorded it never acts again, so a
person who renames the workspace later keeps their name.
"""

from __future__ import annotations

from primer.bootstrap.prime import adopt_legacy_default_workspace
from primer.int.storage_provider import StorageProvider


class M010DefaultWorkspace:
    """Record an existing install's default workspace id and name its row ``prime`` for display."""

    version = 10
    description = "Record the default workspace id and name it prime"

    async def apply(self, sp: StorageProvider) -> None:
        await adopt_legacy_default_workspace(sp)


__all__ = ["M010DefaultWorkspace"]
