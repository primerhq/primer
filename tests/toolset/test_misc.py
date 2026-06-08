import pytest

from primer.toolset.misc import build_misc_toolset
from tests.toolset._desc_conformance import assert_tool_conforms

pytestmark = pytest.mark.asyncio


async def test_misc_tools_conform():
    provider = build_misc_toolset()
    count = 0
    async for tool in provider.list_tools():
        assert_tool_conforms(tool)
        count += 1
    assert count == 6
