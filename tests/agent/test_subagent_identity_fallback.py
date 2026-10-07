"""A subagent with no inherited identity fails closed (security review A-20).

``system__invoke_agent`` is a ``user``-floor tool. Over the MCP endpoint the handler gets no ``ToolContext``, so the subagent's
resume context carries no ``initiated_by``. :func:`primer.agent.invoke.build_subagent_toolmanager` used to fall back to the
system principal, which clears every role floor: a ``role=user`` MCP caller could invoke an agent whose tools include admin-only
system tools and have them run. The fallback is now an ordinary-user rank.
"""

from __future__ import annotations

from types import SimpleNamespace

from primer.agent.invoke import build_subagent_toolmanager
from primer.authz import _role_allows
from primer.model.principal import PrincipalRef


def _context(initiated_by):
    return SimpleNamespace(
        tools=[], session_id=None, workspace_id=None, chat_id="chat-1", principal=None,
        initiated_by=initiated_by, turn_no=None,
    )


async def test_a_subagent_with_no_inherited_identity_is_not_ranked_as_system() -> None:
    manager = await build_subagent_toolmanager(_context(None), provider_registry=None)

    who = manager._initiated_by  # noqa: SLF001
    assert who is not None and who.type != "system"
    assert who.role == "user"
    assert not _role_allows(who, "admin")


async def test_an_inherited_identity_is_kept() -> None:
    admin = PrincipalRef(type="user", id="u-1", display="u", role="admin", source="local")

    manager = await build_subagent_toolmanager(_context(admin), provider_registry=None)

    assert manager._initiated_by == admin  # noqa: SLF001
