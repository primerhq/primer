"""Seglog Task 1: every reader of the session message log, pinned on the single-file log and compared on segmented ones (see ``tests/_support/segmented_log.py``).

Regenerate the pins deliberately: ``PRIMER_CAPTURE_SEGMENTED_GOLDEN=1 uv run pytest tests/session/test_segmented_readers_golden.py -k single_file``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests._support.segmented_log import KNOWN_SEGMENT_GAPS, PLANS, READERS, install, open_env, run_readers, scenario_logs, split_log

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

PINS = Path(__file__).parent / "fixtures" / "segmented_readers"
LOGS = scenario_logs()


def _pin(name: str) -> Path:
    return PINS / f"{name}.json"


async def _answers(tmp_path, raw: bytes, plan: str) -> tuple[dict, int]:
    env = await open_env(tmp_path)
    try:
        parts = split_log(raw, PLANS[plan](raw))
        install(env, parts)
        return await run_readers(env), len(parts)
    finally:
        await env.close()


@pytest.mark.parametrize("name", sorted(LOGS))
async def test_single_file_answers_match_their_pins(name, tmp_path):
    got, parts = await _answers(tmp_path, LOGS[name], "one")
    assert parts == 1
    if os.environ.get("PRIMER_CAPTURE_SEGMENTED_GOLDEN"):
        PINS.mkdir(parents=True, exist_ok=True)
        _pin(name).write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        return
    want = json.loads(_pin(name).read_text())
    assert set(got) == set(want) == set(READERS)
    assert {r: v for r, v in got.items() if v != want[r]} == {}


@pytest.mark.parametrize("plan", [p for p in PLANS if p != "one"])
@pytest.mark.parametrize("name", sorted(LOGS))
async def test_segmented_answers_match_for_every_reader_that_is_segment_aware(name, plan, tmp_path):
    got, parts = await _answers(tmp_path, LOGS[name], plan)
    if parts == 1:
        pytest.skip("the plan leaves this log in one segment")
    want = json.loads(_pin(name).read_text())
    differing = {r for r in READERS if got[r] != want[r]}
    assert differing <= KNOWN_SEGMENT_GAPS, f"readers that must already read segments but differ: {sorted(differing - KNOWN_SEGMENT_GAPS)}"


async def test_every_known_gap_is_a_real_gap(tmp_path_factory):
    """A reader listed in KNOWN_SEGMENT_GAPS must differ for at least one log and plan; otherwise delete it from the list (the list only shrinks)."""
    seen: set[str] = set()
    for name in sorted(LOGS):
        want = json.loads(_pin(name).read_text())
        got, parts = await _answers(tmp_path_factory.mktemp(name), LOGS[name], "every_record")
        if parts > 1:
            seen |= {r for r in READERS if got[r] != want[r]}
    assert KNOWN_SEGMENT_GAPS <= seen, f"listed as a gap but never differs: {sorted(KNOWN_SEGMENT_GAPS - seen)}"


def test_the_plans_actually_cut():
    raw = LOGS["full_run"]
    for plan in PLANS:
        parts = split_log(raw, PLANS[plan](raw))
        assert b"".join(parts) == raw
        assert (len(parts) == 1) == (plan == "one")
