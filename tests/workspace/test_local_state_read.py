"""Tests for LocalStateRepo.read_state_file (Task 1.2)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from primer.workspace.local.state import LocalStateRepo


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


@pytest.fixture
async def state_repo(tmp_path: Path) -> LocalStateRepo:
    repo = LocalStateRepo(tmp_path / ".state", workspace_id="ws-test")
    await repo.initialize()
    return repo


class TestReadStateFile:
    @pytest.mark.asyncio
    async def test_returns_bytes_for_existing_file(
        self, state_repo: LocalStateRepo
    ) -> None:
        payload = json.dumps({"iteration": 1, "status": "running"}).encode()
        await state_repo.commit_arbitrary(
            summary="test: write graphs/g1/state.json",
            files={"graphs/g1/state.json": payload},
        )

        result = await state_repo.read_state_file("graphs/g1/state.json")

        assert result == payload

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_file(
        self, state_repo: LocalStateRepo
    ) -> None:
        result = await state_repo.read_state_file("does/not/exist.json")

        assert result is None
