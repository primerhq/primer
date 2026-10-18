"""Migration 9: stored Docker workspace templates name the runtime image they already ran.

Until the platform revamp of 2026-10 the Docker adapter IGNORED the image a template named: ``DockerRuntimeAdapter.create_sandbox``
always ran ``primer/workspace-runtime:1.1`` and only recorded the typed value in a container label (design 14.2). So the ``image``
stored on a ``container`` template is, in many rows, a value nobody ever ran: ``python:3.12`` typed into a form that did nothing,
or an empty string. The adapter now runs the image a template names, which must contain the primer workspace runtime. A stored
template that still said ``python:3.12`` would, from the first workspace created after the upgrade, start ``python:3.12``, whose
entrypoint is not the runtime, and fail 30 seconds later. Nothing that worked would still work.

So every stored Docker template is rewritten to the image it was already running, ``DOCKER_RUNTIME_IMAGE``
(``primer/workspace-runtime:1.1``), before the image is honoured (design 20 Q6: "Existing Docker templates are rewritten to the
runtime image they were already running. Nothing that worked breaks; the typed value was never used."). From then on the image is
the template's own. A row whose ``backend.kind`` is not ``container`` is not touched (a Kubernetes template's image always ran), and
neither is one that has no ``image`` key (the live model defaults it to the same runtime image) or already names it.

**Why this module redefines WorkspaceTemplate.** The migration must write the row back exactly as it was except for ``backend.image``:
the live model would mask the password of a ``url`` file source and the values of ``env`` in the JSON-mode dump, would drop a key
it does not know, and would fail outright on a row from a shape it no longer reads. The shadow class below reads every field
untyped (``extra="allow"``; the table is selected by the class NAME, as in m007 and m008) and keeps them all as they are, secrets in
the clear, as the storage layer holds them. Only the ``backend`` dict is copied with a new ``image``.

The log names the template id and the images (``template_id``, ``previous_image``, ``image``) and never anything else: not the env, not
a file source, not a url. Idempotent: a moved row already names the runtime image, so a second run finds nothing to do.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage
from primer.model.workspace import DOCKER_RUNTIME_IMAGE

logger = logging.getLogger(__name__)

_PAGE = 200


class WorkspaceTemplate(Identifiable):  # noqa: N801 - the class name selects the storage table
    """Migration-local view of a template row: ``backend`` is read untyped (a row of a shape the live model no longer reads must not
    stop the boot), and every other field is kept exactly as stored."""

    model_config = ConfigDict(extra="allow")

    backend: Any = Field(default=None)


def _previous_image(backend: Any) -> tuple[bool, Any]:
    """``(True, the image it names)`` for a container backend that names an image other than the runtime image, else ``(False, None)``."""
    if not isinstance(backend, dict) or backend.get("kind") != "container" or "image" not in backend:
        return False, None
    image = backend["image"]
    if image == DOCKER_RUNTIME_IMAGE:
        return False, None
    return True, image


class M009DockerTemplateRuntimeImage:
    """Rewrite every stored Docker workspace template to the runtime image it already ran."""

    version = 9
    description = "Docker workspace templates name the runtime image they already ran"

    async def apply(self, sp: StorageProvider) -> None:
        storage = sp.get_storage(WorkspaceTemplate)
        rows: list[WorkspaceTemplate] = []
        offset = 0
        while True:
            page = await storage.list(OffsetPage(offset=offset, length=_PAGE))
            rows.extend(page.items)
            if len(page.items) < _PAGE:
                break
            offset += _PAGE
        for row in rows:
            moved, previous = _previous_image(row.backend)
            if not moved:
                continue
            await storage.update(row.model_copy(update={"backend": {**row.backend, "image": DOCKER_RUNTIME_IMAGE}}))
            logger.warning(
                "moved a Docker workspace template to the runtime image it already ran",
                extra={
                    "template_id": row.id,
                    "previous_image": previous if isinstance(previous, str) else repr(previous),
                    "image": DOCKER_RUNTIME_IMAGE,
                },
            )


__all__ = ["M009DockerTemplateRuntimeImage"]
