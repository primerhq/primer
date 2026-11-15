"""Explicit manual real-model case; outside the default tests/ collection."""
import os
from pathlib import Path

import pytest

from .probe import run


@pytest.mark.requires_llm
@pytest.mark.asyncio
async def test_real_model_watch_survives_compaction_and_active_lease_crash():
    await run(Path(os.environ['W4_F1_OUTPUT']))
