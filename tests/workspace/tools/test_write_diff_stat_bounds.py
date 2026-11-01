"""The write tool's diff stat: exact below the size cap, skipped above it, and never computed on the event loop (W4, ruling D11)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import primer.workspace as _workspace_pkg  # noqa: F401  (ToolCallContext.model_rebuild)
from primer.workspace.local.tools import write as write_mod
from primer.workspace.local.tools.write import Write, WriteArgs, _diff_stat


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(session=SimpleNamespace(was_read=lambda p: True, mark_read=lambda p: None))


def _config_like(n: int) -> list[str]:
    return [("  }," if i % 3 == 0 else ("" if i % 3 == 1 else f"  key{i}: {i}")) for i in range(n)]


# --- characterization: the stat below the cap is what it was ---


def test_stat_values_below_the_cap_are_unchanged() -> None:
    old = _config_like(300)
    new = list(old)
    for i in range(2, 300, 15):
        new[i] = new[i] + " # edited"
    new.insert(10, "inserted")
    del new[200:205]
    assert _diff_stat(old, new) == {"additions": 21, "deletions": 25}
    old2 = [("    }," if i % 2 else f'  "k{i}": {i},') for i in range(200)]
    new2 = [("    }," if i % 2 else f'  "j{i}": {i},') for i in range(200)]
    assert _diff_stat(old2, new2) == {"additions": 100, "deletions": 100}
    assert _diff_stat([], ["a", "b"]) == {"additions": 2, "deletions": 0}
    assert _diff_stat(["a"], []) == {"additions": 0, "deletions": 1}
    assert _diff_stat(["a"], ["a"]) == {}
    assert _diff_stat(None, ["a"]) == {}


async def test_overwrite_reports_the_stat(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\nc\n")
    result = await Write(tmp_path).execute(WriteArgs(path="f.txt", content="a\nx\nc\nd\n"), _ctx())
    assert result.output == "wrote 8 bytes to f.txt"
    assert result.metadata == {"additions": 2, "deletions": 1}


# --- W4: skipped past the cap, and off the loop ---


def test_stat_past_the_cap_is_skipped_and_says_so() -> None:
    old = [f"old {i}" for i in range(2001)]
    new = [f"new {i}" for i in range(2001)]
    assert _diff_stat(old, new) == {"diff_stat_skipped": True}


async def test_a_slow_stat_does_not_stop_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    def slow_stat(old, new):
        time.sleep(0.5)
        return {"additions": 1, "deletions": 0}

    monkeypatch.setattr(write_mod, "_diff_stat", slow_stat)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        result = await Write(tmp_path).execute(WriteArgs(path="f.txt", content="x\n"), _ctx())
    finally:
        task.cancel()
    assert result.metadata == {"additions": 1, "deletions": 0}
    assert ticks >= 10, ticks
