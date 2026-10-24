"""Migration 18: remove the shipped gate on ``crud::list_python_tools``, a read (ticket 01a16f1d ruling 4).

The ensure pass seeded a ``required`` policy for every tool of the crud toolset. The crud gates are for the builder's platform writes, and
``list_python_tools`` writes nothing: it derives a Python toolset's tools from the stored source. So it parked for approval with nothing to approve.
The ensure pass no longer seeds it (``crud.CRUD_GATED_TOOL_NAMES``); this step removes the row an older seed wrote, so an upgraded install holds the
gates a fresh one does:

* the row under the seeded id (``tool-approval-policy-crud-list-python-tools``) is deleted only while it is exactly as seeded: enabled, an unconditional
  ``required`` gate, no timeout, no approvers, no preview paths;
* a row the operator changed (disabled it, retuned it, named its approvers, made it a Rego or LLM gate), and a policy the operator wrote under another
  id, is the operator's gate and stays.

What the step reads is frozen here, as it was when it shipped: the table, the seeded id and the seeded shape. Delete-only, and a row already gone is
skipped (``NotFoundError``), so a second run, or a run that died half way, changes nothing more. The view redefines ``ToolApprovalPolicy`` so a stored
row the live model may reject is still read (see m002/m003).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import NotFoundError

logger = logging.getLogger(__name__)

_SEEDED_ID = "tool-approval-policy-crud-list-python-tools"

# The row as the ensure pass wrote it (``enabled`` defaulted to true, every optional field to null).
_SEEDED: dict[str, Any] = {
    "toolset_id": "crud",
    "tool_name": "list_python_tools",
    "enabled": True,
    "approval": {"type": "required"},
    "timeout_seconds": None,
    "approvers": None,
    "preview_args": None,
}


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - the class name selects the table
    """Migration-local view: every stored field kept as it is."""

    model_config = ConfigDict(extra="allow")


def _as_seeded(row: ToolApprovalPolicy) -> bool:
    """Whether the stored row is the seeded gate, unchanged. A field the row does not carry reads as its default (an older row may omit one)."""
    stored = row.model_dump()
    defaults = {"enabled": True, "timeout_seconds": None, "approvers": None, "preview_args": None}
    return all(stored.get(key, defaults.get(key)) == value for key, value in _SEEDED.items())


class M018UngateListPythonTools:
    """Delete the seeded, unchanged gate on ``crud::list_python_tools``."""

    version = 18
    description = "Remove the shipped approval gate on crud::list_python_tools, a read"

    async def apply(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ToolApprovalPolicy)
        row = await store.get(_SEEDED_ID)
        if row is None:
            return
        if not _as_seeded(row):
            logger.info("left the operator's gate on crud::list_python_tools as it is", extra={"policy": _SEEDED_ID})
            return
        try:
            await store.delete(_SEEDED_ID)
        except NotFoundError:
            return
        logger.info("removed the shipped gate on crud::list_python_tools, a read", extra={"policy": _SEEDED_ID})


__all__ = ["M018UngateListPythonTools"]
