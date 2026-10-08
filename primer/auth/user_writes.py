"""Field-scoped writes to a :class:`User` row (SEC-05 review).

The auth routes read the user, await argon2 (tens of milliseconds), and
used to write the WHOLE document back with ``storage.update``. Anything
another request committed in that gap was lost: a login finishing after a
sign-out-everywhere put the old ``session_epoch`` back and so re-validated
the cookies the user had just revoked; a stale admin edit did the same; a
stale sign-out could undo a concurrent ``disabled=True``.

Every writer here touches only its own fields through
:meth:`Storage.patch_if`, so a concurrent writer's other fields always
survive. A write that moves ``session_epoch`` is a compare-and-set on the
epoch it read, re-read and retried when another bump got there first, so
two bumps never collapse into one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from primer.int.storage import Storage
from primer.model.common import dump_for_storage
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.user import User
from primer.storage._patch import raw_generation
from primer.storage.cas import patch_if_checked

_EPOCH_ATTEMPTS = 8


def _stored(user: User, fields: Mapping[str, Any]) -> dict[str, Any]:
    """``fields`` in the JSON form the storage layer writes for them."""
    dumped = dump_for_storage(user.model_copy(update=dict(fields)))
    return {name: dumped[name] for name in fields}


async def stamp_login(storage: Storage[User], user: User, *, at: datetime) -> User | None:
    """Write ONLY ``last_login_at``, iff the password just verified and the enabled flag still hold.

    Returns the stored user (its ``session_epoch`` is the one to sign the cookie with), or ``None``
    when the password changed or the account was disabled while the hash was being verified.
    """
    return await patch_if_checked(
        storage, user.id, _stored(user, {"last_login_at": at}),
        where={
            "password_hash": [raw_generation(user, "password_hash")],
            "disabled": [False],
        },
    )


async def write_user_fields(
    storage: Storage[User],
    user_id: str,
    fields: Mapping[str, Any],
    *,
    bump_epoch: bool,
    check: Callable[[User], None] | None = None,
) -> User:
    """Write ``fields`` (and, with ``bump_epoch``, ``session_epoch + 1``) and nothing else.

    The write is guarded on the ``session_epoch`` read just before it; when another writer moved the
    epoch first, the row is re-read and the write retried, so a bump is never lost and never undone.
    ``check`` runs on every fresh read and may raise to abandon the write (for example, the password
    the caller verified is no longer the stored one).
    """
    for _ in range(_EPOCH_ATTEMPTS):
        current = await storage.get(user_id)
        if current is None:
            raise NotFoundError(f"User {user_id!r} does not exist")
        if check is not None:
            check(current)
        update = dict(fields)
        if bump_epoch:
            update["session_epoch"] = current.session_epoch + 1
        if not update:
            return current
        written = await patch_if_checked(
            storage, user_id, _stored(current, update),
            where={"session_epoch": [raw_generation(current, "session_epoch")]},
        )
        if written is not None:
            return written
    raise ConflictError(f"User {user_id!r} kept changing; retry the request")


__all__ = ["stamp_login", "write_user_fields"]
