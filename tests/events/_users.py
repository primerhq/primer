"""Park fixtures: the person a parked session runs as is a real account (a deleted or disabled one takes no event wake)."""
from __future__ import annotations

from datetime import UTC, datetime

from primer.model.principal import PrincipalRef
from primer.model.user import User


async def seed_account_of(sp, ref: PrincipalRef | None) -> None:
    """Create the user row ``ref`` names, at its recorded role, when there is none."""
    if ref is None or ref.type != "user":
        return
    users = sp.get_storage(User)
    if await users.get(ref.id) is None:
        await users.create(User(id=ref.id, username=ref.id, created_at=datetime.now(UTC), role=ref.role))
