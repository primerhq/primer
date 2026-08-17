"""Resolving the binding a session starts on.

Naming an agent at create time stops being mandatory: a session created
without one runs the system default, which is what lets someone open
the console and simply start talking.

With no default configured the create is REJECTED rather than resolved
to whichever agent happens to exist. Guessing there is how a workspace
ends up running something nobody chose, and the failure is trivially
fixable by configuring the default.
"""

from __future__ import annotations

from typing import Any

from primer.model.except_ import ConfigError
from primer.model.workspace_session import AgentSessionBinding

NO_DEFAULT_AGENT_MESSAGE = (
    "no binding given and no default agent is configured "
    "(set system default_agent_id)"
)


async def resolve_initial_binding(
    *, requested: Any | None, storage_provider: Any,
):
    """Return the binding a new session should start on.

    An explicit binding always wins; the default is a fallback, never
    an override.
    """
    if requested is not None:
        return requested

    state = await storage_provider.get_system_state()
    default_agent_id = getattr(state, "default_agent_id", None)
    if not default_agent_id:
        raise ConfigError(NO_DEFAULT_AGENT_MESSAGE)
    return AgentSessionBinding(agent_id=default_agent_id)


__all__ = ["NO_DEFAULT_AGENT_MESSAGE", "resolve_initial_binding"]
