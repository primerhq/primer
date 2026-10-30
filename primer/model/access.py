"""Per-entity access: the Access value stored on owned rows, the levels, and the Owned mixin (access-control spec 6.1, 6.2).

``Access`` is a JSONB field on an entity row (agents, graphs, toolsets,
workspace templates and channels in Phase 1): who owns it, who it is
shared with and at which level, and whether it is public. ``members`` is
the derived list of membership tokens the list queries filter on
(``u:<id>`` per owner and grantee, ``public`` when publicly visible); it
is always recomputed from the other fields and never trusted from a
request body, so a client cannot widen its own visibility. A grant for a
user who is also an owner is dropped, since ownership already covers it.
Levels are ``read`` < ``full`` < ``owner`` (spec 6.2); public always means
read for every full user.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AccessLevel(StrEnum):
    """The three access levels, ordered by privilege (spec 6.2)."""

    READ = "read"
    FULL = "full"
    OWNER = "owner"


#: Privilege rank for level comparisons (higher wins).
LEVEL_RANK = {AccessLevel.READ: 1, AccessLevel.FULL: 2, AccessLevel.OWNER: 3}


class Access(BaseModel):
    """Ownership and sharing for one entity row (spec 6.1).

    ``owners``: user ids, several allowed; the creator starts as the sole
    owner. ``grants``: user id to level for shared access (``read`` or
    ``full``; an entity is never granted ``owner``). ``public`` opens
    read access to every full user (spec 6.2: public always means read);
    ``public_tools`` marks individual tools of a toolset as publicly
    usable (Phase 4's tool_is_public) without exposing the row itself.
    ``members`` is derived and indexed (Task 4's GIN index): it carries a
    ``u:<id>`` token for every owner and grantee and ``public`` when the
    row is publicly visible, which is what the list predicate matches.
    """

    owners: list[str] = []
    grants: dict[str, Literal["read", "full"]] = {}
    public: bool = False
    public_tools: list[str] = []
    members: list[str] = []

    @model_validator(mode="after")
    def _derive_members(self) -> Access:
        owners = set(self.owners)
        # An owner's grant is redundant: drop it so the row cannot carry
        # two different statements about the same user.
        self.grants = {user_id: level for user_id, level in self.grants.items() if user_id not in owners}
        tokens = {f"u:{user_id}" for user_id in owners | set(self.grants)}
        if self.public:
            # Only the row-level flag makes the row listable to every full user.
            # public_tools opens individual tools (Phase 4's tool_is_public) and
            # must NOT leak the row itself into everyone's list.
            tokens.add("public")
        self.members = sorted(tokens)
        return self

    @classmethod
    def owned_by(cls, user_id: str) -> Access:
        """A fresh private row owned by ``user_id`` (the creator)."""
        return cls(owners=[user_id])


class Owned(BaseModel):
    """Mixin for the entities Phase 1 makes owned (spec 6.1)."""

    access: Access = Field(
        default_factory=Access,
        description="Ownership and sharing for this entity; its derived members are indexed for list queries.",
    )
