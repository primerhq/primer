"""What happens to a user's sharing when the account is deleted (access-control Phase 6, Task 1).

:func:`hand_over` walks every owned kind and every grantable kind and removes the user from the ``access`` block of each row that names them:

* an owned row the user SOLELY owns goes to the heir (the admin who deleted them); a co-owned row keeps its other owners and the heir is not added;
* the user's grants are dropped everywhere, and the grants OTHER people hold on the user's rows are kept;
* a grantable row (a model profile, a provider) only loses the use grant;
* a row a harness installed is not touched directly: its harness is handed over and the new access is fanned out to it (the direct ``harness_managed`` refusal is not
  bypassed);
* the triggers that run as the user are DISABLED (their run-as owner is not transferred: the heir would otherwise lend admin access to a webhook anyone can call);
* the user's default workspace becomes an ordinary workspace of the heir, renamed so the heir can tell it from their own.

Every row write is ONE field-scoped ``patch_if`` of ``access`` guarded on its ``rev``, re-read and re-applied on a lost race (a few times at most), so a
sharing edit made meanwhile is never lost. Re-running converges: a row that no longer names the user is not read again.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.access.person import Person
from primer.access.policy import member_predicate
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.channel import Channel, ChannelProvider
from primer.model.collection import Collection
from primer.model.graph import Graph
from primer.model.harness import Harness
from primer.model.model_profile import ModelProfile
from primer.model.providers.cross_encoder import CrossEncoderProvider
from primer.model.providers.embedding import EmbeddingProvider
from primer.model.providers.llm import LLMProvider
from primer.model.providers.toolset import Toolset
from primer.model.providers.vector import SemanticSearchProvider
from primer.model.storage import OffsetPage
from primer.model.trigger import Trigger
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceProvider, WorkspaceTemplate

logger = logging.getLogger(__name__)

#: Every kind with an ``Owned`` access block. A static test pins that a later ``Owned`` model is added here.
OWNED_KINDS: tuple[type, ...] = (Agent, Graph, Toolset, WorkspaceTemplate, Channel, Workspace, Collection, Trigger, Harness)
#: Every kind with a ``Grantable`` (use-only) access block.
GRANTABLE_KINDS: tuple[type, ...] = (
    ModelProfile, LLMProvider, EmbeddingProvider, CrossEncoderProvider, SemanticSearchProvider, WorkspaceProvider, ChannelProvider,
)

_PAGE = 200
_ATTEMPTS = 4


async def heir_for(sp: Any, person: Person) -> str | None:
    """The user who inherits what a deleted user solely owned: the admin who deleted them; with no user behind the request (auth disabled), the oldest enabled admin.

    ``None`` when there is no admin at all: rows are then left as they are (admins reach everything).
    """
    if person.is_admin and person.user_id is not None:
        return person.user_id
    admins: list[User] = []
    offset = 0
    while True:
        page = await sp.get_storage(User).list(OffsetPage(offset=offset, length=_PAGE))
        admins.extend(u for u in page.items if u.role == "admin" and not u.disabled and u.created_at is not None)
        if len(page.items) < _PAGE:
            break
        offset += _PAGE
    return min(admins, key=lambda u: u.created_at).id if admins else None


async def _named_ids(storage: Any, user_id: str) -> list[str]:
    """The ids of the rows whose access names ``user_id``, all read before any write."""
    ids: list[str] = []
    offset = 0
    predicate = member_predicate(user_id)
    while True:
        page = await storage.find(predicate, OffsetPage(offset=offset, length=_PAGE))
        ids.extend(r.id for r in page.items)
        if len(page.items) < _PAGE:
            return ids
        offset += _PAGE


def _without(access: Access, user_id: str, heir_id: str | None, *, owned: bool) -> Access | None:
    """``access`` with ``user_id`` removed (owners handed to the heir when they were the last), or ``None`` when nothing changes."""
    owners = [o for o in access.owners if o != user_id]
    grants = {u: lvl for u, lvl in access.grants.items() if u != user_id}
    if owned and not owners and user_id in access.owners:
        if heir_id is None:
            return None  # nobody to inherit: the row stays as it is (admins reach everything)
        owners = [heir_id]
        grants.pop(heir_id, None)
    if owners == access.owners and grants == access.grants:
        return None
    return Access(owners=owners, grants=grants, public=access.public, public_tools=list(access.public_tools), rev=access.rev + 1)


async def _rewrite(storage: Any, row_id: str, user_id: str, heir_id: str | None, *, owned: bool) -> Any | None:
    """Remove the user from one row, guarded on the ``access`` rev; ``None`` when the row needed no change (or is managed by a harness)."""
    for _ in range(_ATTEMPTS):
        row = await storage.get(row_id)
        if row is None or (owned and getattr(row, "harness_id", None)):
            return None
        new = _without(row.access, user_id, heir_id, owned=owned)
        if new is None:
            return None
        rev = row.access.rev
        guard = [rev, None] if rev == 0 else [rev]
        written = await storage.patch_if(row_id, {"access": new.model_dump(mode="json")}, where={("access", "rev"): guard})
        if written is not None:
            return written
    logger.warning("hand-over of %s lost its race %d times; the repeated delete finishes it", row_id, _ATTEMPTS)
    return None


async def hand_over(sp: Any, user_id: str, heir_id: str | None) -> dict[str, int]:
    """Remove ``user_id`` from the sharing of everything and hand what they solely owned to ``heir_id``; returns the number of rows changed per kind."""
    from primer.harness.access import apply_harness_access

    summary: dict[str, int] = {}
    for model in (*OWNED_KINDS, *GRANTABLE_KINDS):
        owned = model in OWNED_KINDS
        storage = sp.get_storage(model)
        changed = 0
        for row_id in await _named_ids(storage, user_id):
            written = await _rewrite(storage, row_id, user_id, heir_id, owned=owned)
            if written is None:
                continue
            changed += 1
            if model is Harness:
                await apply_harness_access(sp, written)  # what it installed follows it
        summary[model.__name__] = changed
    # Rows a harness installed still name the user if their harness did not change in this pass (a copy of an access that never named them is untouched).
    summary["Trigger.disabled"] = await _disable_triggers_run_as(sp, user_id)
    summary["Workspace.renamed"] = await _rename_default_workspace(sp, user_id)
    return summary


async def _disable_triggers_run_as(sp: Any, user_id: str) -> int:
    """Disable the triggers whose run-as owner is the user (their next fire would be refused anyway; this stops it failing on every fire)."""
    storage = sp.get_storage(Trigger)
    from primer.storage.q import Q

    count = 0
    offset = 0
    ids: list[str] = []
    predicate = Q(Trigger).where("owner.id", user_id).where("enabled", True).build()
    while True:
        page = await storage.find(predicate, OffsetPage(offset=offset, length=_PAGE))
        ids.extend(r.id for r in page.items)
        if len(page.items) < _PAGE:
            break
        offset += _PAGE
    for trigger_id in ids:
        if await storage.patch_if(trigger_id, {"enabled": False, "next_fire_at": None}, where={"enabled": [True]}) is not None:
            count += 1
    return count


async def _rename_default_workspace(sp: Any, user_id: str) -> int:
    """The user's per-user default workspace becomes an ordinary workspace (renamed ``<username> (deleted user)``); 0 when there is none."""
    user = await sp.get_storage(User).get(user_id)
    workspace_id = getattr(user, "default_workspace_id", None) if user is not None else None
    if not workspace_id:
        return 0
    storage = sp.get_storage(Workspace)
    row = await storage.get(workspace_id)
    if row is None:
        return 0
    name = f"{user.username} (deleted user)"
    # Guarded on the name just read (a rename in between wins; the id is a column and cannot be named in a guard).
    return 1 if row.name == name or await storage.patch_if(workspace_id, {"name": name}, where={"name": [row.name]}) is not None else 0


__all__ = ["GRANTABLE_KINDS", "OWNED_KINDS", "hand_over", "heir_for"]
