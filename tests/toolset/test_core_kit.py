"""Task 12: the five-tool core kit is user-role and complete in the catalog.

P2 grants ``CORE_KIT_TOOLS`` to the seeded agents and Plan 05's agent form
"Core kit" preset copies it; what both rely on is that every id in the tuple
is a real, catalog-listed tool an ordinary user run may call.
"""

from __future__ import annotations

from primer.toolset.core_kit import CORE_KIT_TOOLS
from primer.toolset.tool_catalog import collect_tool_entries
from tests.docs._live_toolsets import built_in_providers


async def test_every_core_kit_tool_is_callable_by_a_user_run():
    entries = {e.scoped_id: e for e in await collect_tool_entries(built_in_providers())}
    assert len(CORE_KIT_TOOLS) == 5
    assert {tid: entries[tid].required_role for tid in CORE_KIT_TOOLS} == dict.fromkeys(
        CORE_KIT_TOOLS, "user"
    )
