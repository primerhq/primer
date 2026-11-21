"""Migration 35: seed the workspace service publisher's admin-only gate.

Frozen create-only policy for system::publish_service. Existing operator rows
under any id and either system spelling win, including disabled/custom policies.
M016's historical table is unchanged. A create conflict preserves the winner;
no existing row is retuned. The runner stamps completion only after success.
"""
from __future__ import annotations

import logging

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)
_POLICY_ID = "tool-approval-policy-system-publish-service"
_TOOL_NAME = "publish_service"
_ADMIN_ONLY = {"kind": "roles", "roles": [], "users": []}


class ToolApprovalPolicy(Identifiable):  # noqa: N801 - name selects the existing table
    model_config = ConfigDict(extra="allow")
    toolset_id: str
    tool_name: str


async def _has_policy(store) -> bool:
    # Semantic policy identity wins regardless of an unrelated seed-ID occupant.
    for spelling in ("system", "_system"):
        query = Q(ToolApprovalPolicy).where("toolset_id", spelling).where("tool_name", _TOOL_NAME).build()
        if (await store.find(query, OffsetPage(offset=0, length=1))).items:
            return True
    row = await store.get(_POLICY_ID)
    if row is not None:
        # With no semantic publisher policy, an occupied seed ID cannot be
        # overwritten or counted as a successful gate installation.
        raise ConflictError("workspace publisher approval id belongs to another tool")
    return False


class M035WorkspacePublishGate:
    version = 35
    description = "Gate workspace service publication with required admin-only approval"

    async def apply(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ToolApprovalPolicy)
        if await _has_policy(store):
            return
        try:
            await store.create(ToolApprovalPolicy(
                id=_POLICY_ID, toolset_id="system", tool_name=_TOOL_NAME,
                enabled=True, approval={"type": "required"}, approvers=dict(_ADMIN_ONLY),
            ))
        except ConflictError:
            if not await _has_policy(store):
                raise
            return
        logger.info("created workspace service publication approval gate", extra={"policy": _POLICY_ID})
