"""E2E: LLM-judge policy parks sensitive calls, lets benign ones through.

Skipped until the portable stub-LLM harness lands; unit tests
(`tests/agent/test_approval_gate.py`) cover the judge logic comprehensively.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_llm_judge_full_cycle() -> None:
    pytest.skip(
        "E2E LLM-judge journey scheduled to land alongside the "
        "portable stub-LLM harness; unit tests cover the judge "
        "logic comprehensively."
    )
