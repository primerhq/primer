"""The ToolContext a system tool sees when a given identity runs it (a run's ``initiated_by``).

The system CRUD tools that gate a write on the CALLER (a stdio MCP toolset needs an admin, architecture review A-02) read
``ctx.initiated_by``; a test that writes such a row says who is writing it with :func:`caller`, instead of calling with no identity,
which is refused.
"""

from __future__ import annotations

from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext


def caller(role: str | None = "admin", *, kind: str = "user") -> ToolContext:
    """A context whose run was started by a ``kind`` actor holding ``role`` (``None`` for the role-less internal actors)."""
    who = PrincipalRef(type=kind, id=f"{kind}-1", display=kind, role=role, source="local")  # type: ignore[arg-type]
    return ToolContext(tool_call_id="tc", session_id="s", workspace_id="w", initiated_by=who)


ADMIN_CALLER = caller("admin")
