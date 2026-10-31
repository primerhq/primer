"""``create_workspace_session`` refuses caller metadata that names a system key (security review GS-1).

The tool shares ``start_workspace_session`` with the REST route, so a forged ``reply_binding`` or ``subscription_id`` is refused
here too, while ordinary keys are stored as sent.
"""

from __future__ import annotations

import json

import pytest

from tests.toolset.test_workspaces_session_attribution import (  # noqa: F401
    _actor, seeded, session_toolset, sp, workspace_registry,
)

AGENT = {"kind": "agent", "agent_id": "code-reviewer"}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["reply_binding", "subscription_id"])
async def test_a_reserved_key_is_refused(session_toolset, seeded, key) -> None:  # noqa: F811
    result = await session_toolset.call(
        tool_name="create_workspace_session",
        arguments={"workspace_id": seeded, "binding": AGENT, "metadata": {key: {"channel_id": "ch-x"}}},
    )
    assert result.is_error
    assert key in result.output


@pytest.mark.asyncio
async def test_ordinary_metadata_is_stored(session_toolset, seeded) -> None:  # noqa: F811
    result = await session_toolset.call(
        tool_name="create_workspace_session",
        arguments={"workspace_id": seeded, "binding": AGENT, "metadata": {"ticket": "INC-1"}},
    )
    assert not result.is_error, result.output
    assert json.loads(result.output)["metadata"] == {"ticket": "INC-1"}
