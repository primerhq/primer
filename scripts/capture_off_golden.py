#!/usr/bin/env python3
"""Regenerate tests/agent/fixtures/off_mode_golden.json: today's compaction behaviour, recorded.

The fixture pins what the prompt-budget work must NOT change under its ``off`` mode (see
``tests/_support/off_golden.py`` for the scenario and ``tests/agent/test_off_mode_golden.py`` for
the comparison). It is only worth anything if it was captured from the code it claims to pin, so
run this from a CLEAN checkout of that commit and commit the result before changing anything
under ``primer/``. The script records the commit it ran at and refuses to run when ``primer/``
has uncommitted changes (``--allow-dirty`` overrides that for a deliberate re-capture).

    git worktree add ../pin <sha> && cd ../pin
    python scripts/capture_off_golden.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Run as a script, sys.path[0] is scripts/, which has its own ``tests`` package and would shadow the
# repository's: drop it, and put the repository root first.
sys.path[:] = [str(ROOT), *(p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent)]

from tests._support.off_golden import run_scenario  # noqa: E402

FIXTURE = ROOT / "tests" / "agent" / "fixtures" / "off_mode_golden.json"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--allow-dirty", action="store_true", help="capture even if primer/ has uncommitted changes")
    args = parser.parse_args(argv)
    commit = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain", "--", "primer")
    if dirty and not args.allow_dirty:
        print(f"refusing to capture: primer/ has uncommitted changes, so the fixture would not describe {commit}:\n{dirty}",
              file=sys.stderr)
        return 1
    result = asyncio.run(run_scenario())
    fixture = {"captured_from": commit, **result}
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE.relative_to(ROOT)} from {commit}: {result['call_count']} LLM calls, {len(result['turns'])} turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
