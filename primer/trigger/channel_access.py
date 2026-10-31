"""The channel READ a channel trigger needs on the channel it fires from (access-control Phase 3, Decision 5).

A channel trigger's events ARE channel content, so the person who saves the trigger - or binds a
subscription onto it - must hold ``read`` on the channel it fires from. A channel the person holds
no level on is refused exactly like a missing one (``NotFoundError``, spec 9: no leak of "exists
but not yours"), and a provider-wide config (``channel_id`` null) listens to every channel the
provider has, so it is admin-only (``AccessDeniedError``: 403 ``access_denied``).

Every save door calls this before it stores: the REST create/update routes, the trigger tools
(which the crud toolset re-exports) and ``system__create_channel_binding``. The check at bind time
is the gate; nothing in ``primer.channel.event_dispatch`` is involved (P3 plan, Task 5).
"""

from __future__ import annotations

from typing import Any

from primer.access.person import Person
from primer.access.policy import require
from primer.model.access import AccessLevel
from primer.model.channel import Channel
from primer.model.except_ import AccessDeniedError, NotFoundError
from primer.model.trigger import ChannelTriggerConfig


async def require_channel_trigger_access(sp: Any, person: Person, config: Any) -> None:
    """Refuse a channel trigger the person may not save (P3 Decision 5); a no-op for every other config kind."""
    if not isinstance(config, ChannelTriggerConfig):
        return
    if config.channel_id is None:
        # Provider-wide: it would listen to every channel the provider has, so only an admin may save it.
        if not person.is_admin:
            raise AccessDeniedError(
                "a channel trigger without a channel_id is admin-only"
            )
        return
    channel = await sp.get_storage(Channel).get(config.channel_id)
    if channel is None:
        raise NotFoundError(f"Channel {config.channel_id!r} does not exist")
    require(person, channel.access, AccessLevel.READ, what=f"Channel {config.channel_id!r}")
