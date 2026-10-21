"""Migration 12: snapshot each workspace's template for durable re-attachment.

Workspace rows used to keep only ``template_id``. Re-attachment therefore
loaded the current template row, so an edit changed an existing workspace and
a delete made re-attachment impossible. This migration copies the full raw
template document into each existing workspace row.

The migration-local views keep unknown fields and leave nested values untyped.
That preserves template secrets exactly as storage holds them and avoids
depending on fields the live models may add later. Only ``template_snapshot``
is patched, so concurrent changes to workspace metadata are not overwritten.
Rows whose template is already missing are logged and left with ``None``; they
were already unable to re-attach on the old path.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable, dump_for_storage
from primer.model.storage import OffsetPage
from primer.storage import raw_generation

logger = logging.getLogger(__name__)

_PAGE = 200


class WorkspaceTemplate(Identifiable):  # noqa: N801 - class name selects the table
    """Migration-local raw view of a template row."""

    model_config = ConfigDict(extra="allow")


class Workspace(Identifiable):  # noqa: N801 - class name selects the table
    """Migration-local view of the provenance id and new snapshot field."""

    model_config = ConfigDict(extra="allow")

    template_id: str
    template_snapshot: Any = Field(default=None)


class M012WorkspaceTemplateSnapshots:
    """Copy the create-time template document onto legacy workspace rows."""

    version = 12
    description = "Snapshot each workspace's template for durable re-attachment"

    async def apply(self, sp: StorageProvider) -> None:
        workspaces = sp.get_storage(Workspace)
        templates = sp.get_storage(WorkspaceTemplate)
        offset = 0
        while True:
            page = await workspaces.list(OffsetPage(offset=offset, length=_PAGE))
            for row in page.items:
                if row.template_snapshot is not None:
                    continue
                template = await templates.get(row.template_id)
                if template is None:
                    logger.warning(
                        "workspace template snapshot migration skipped a missing template",
                        extra={"workspace_id": row.id, "template_id": row.template_id},
                    )
                    continue
                await workspaces.patch_if(
                    row.id,
                    {"template_snapshot": dump_for_storage(template)},
                    where={
                        "template_id": [raw_generation(row, "template_id")],
                        "template_snapshot": [None],
                    },
                )
            if len(page.items) < _PAGE:
                break
            offset += _PAGE


__all__ = ["M012WorkspaceTemplateSnapshots"]
