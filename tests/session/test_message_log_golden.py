"""Stage 0 of Task 14: the golden transcripts of the session message log (see ``tests/_support/log_golden.py``).

Regenerate deliberately with ``PRIMER_CAPTURE_LOG_GOLDEN=1`` (one file per scenario under ``fixtures/message_log_golden/``).

Every later stage (the cached read side, the append-only write behind ``session_log_append_only``) must reproduce these byte for byte.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests._support.golden_compare import differences
from tests._support.log_golden import SCENARIOS, capture

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

FIXTURES = Path(__file__).parent / "fixtures" / "message_log_golden"


def _path(name: str) -> Path:
    return FIXTURES / f"{name}.json"


@pytest.mark.parametrize("append_only", [False, True])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_the_transcript_matches_its_golden(name, append_only, tmp_path):
    got = await capture(name, tmp_path, append_only=append_only)
    if os.environ.get("PRIMER_CAPTURE_LOG_GOLDEN") and not append_only:   # the flag-off run is the golden; flag-on is compared with it
        FIXTURES.mkdir(parents=True, exist_ok=True)
        _path(name).write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        return
    want = json.loads(_path(name).read_text())
    assert differences(got, want) == []


async def test_every_scenario_has_a_golden():
    assert sorted(p.stem for p in FIXTURES.glob("*.json")) == sorted(SCENARIOS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_a_scenario_is_deterministic(name, tmp_path):
    first = await capture(name, tmp_path / "a")
    second = await capture(name, tmp_path / "b")
    assert differences(first, second) == []
