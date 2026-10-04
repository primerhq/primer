#!/usr/bin/env python3
"""Regenerate tests/agent/fixtures/off_mode_golden.json: today's compaction behaviour, recorded.

The fixture pins what the prompt-budget work must NOT change under its ``off`` mode (see
``tests/_support/off_golden.py`` for the scenario and ``tests/agent/test_off_mode_golden.py`` for
the comparison). It is only worth anything if it was captured from the code it claims to pin, so
run this with ``primer/`` COMMITTED and clean. The fixture records the ``primer/`` TREE hash it ran
against (``git rev-parse HEAD:primer``), which survives a rebase-merge or a squash (a commit SHA
does not), and the script refuses to run when ``primer/`` has uncommitted changes (``--allow-dirty``
overrides that for a deliberate re-capture). Only ``primer/`` is checked: the scenario and this
script live in ``tests/`` and ``scripts/``, so they may differ from the pinned commit's.

To re-capture AT the change that makes the fixture move (the usual case): commit the ``primer/``
change, run the script from that checkout, and put the new fixture in the SAME commit
(``git commit --amend``; the tree hash of ``primer/`` does not change when ``tests/`` does).

To reproduce an OLD fixture (proof that it described its pinned code): check out the pinned commit,
copy ``tests/_support/off_golden.py`` and this script from the branch if they do not exist there,
and run it; the output must equal the committed fixture byte for byte except ``captured_from``.

    git worktree add .claude/worktrees/pin <sha> && cd .claude/worktrees/pin
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
    # The primer/ TREE hash, not the commit: rebase-merge orphans commit SHAs, while the tree of primer/ is
    # the same object after a rebase, a squash or a cherry-pick of the commit that produced it.
    tree = _git("rev-parse", "HEAD:primer")
    dirty = _git("status", "--porcelain", "--", "primer")
    if dirty and not args.allow_dirty:
        print(f"refusing to capture: primer/ has uncommitted changes, so the fixture would not describe tree {tree}:\n{dirty}",
              file=sys.stderr)
        return 1
    result = asyncio.run(run_scenario())
    fixture = {"captured_from": tree, **result}
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE.relative_to(ROOT)} from primer/ tree {tree}: {result['call_count']} LLM calls, {len(result['turns'])} turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
