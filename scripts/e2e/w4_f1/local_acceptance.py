"""One explicit guest case, outside all ordinary collection. No automatic VM/model launch."""
import os
from pathlib import Path

import pytest

from .local_vm import run


@pytest.mark.requires_llm
async def test_real_model_watch_survives_compaction_and_active_lease_crash():
    await run(Path(os.environ['W4_F1_LOCAL_OUTPUT']), Path(os.environ['W4_F1_LOCAL_AUTHORIZATION']),
              os.environ['W4_F1_LOCAL_POSTGRES_DSN'])
