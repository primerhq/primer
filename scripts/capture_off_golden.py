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

A re-capture is GUARDED. Behaviour is compared turn by turn (``tests/_support/golden_compare.py``: a
turn's unit is its record plus its own calls, so one turn's change does not renumber the next), and the
script refuses to write when:

* a turn changed that you did not declare: ``--expect-changed 2,3`` names the turns (1-based) allowed to
  change, and everything else must be byte-identical;
* a turn you declared did not change;
* turns are declared without ``--reason "why"``.

It then records ``{previous_fixture_sha256, previous_captured_from, changed_turns, reason}`` on the
fixture's ``recaptures`` list, so the history of why the fixture moved is in the fixture. When nothing but
the capture metadata differs (the ``primer/`` tree moved elsewhere) it updates ``captured_from`` alone.
``--check`` never writes: it prints which turns differ (with every differing path) and exits non-zero
unless they are exactly the declared ones. A first capture needs ``--init``.

MERGE-BASE REPRODUCTION CHECK (do this before declaring a re-capture): on a checkout of the merge base
with the NEW scenario and script copied in (``git worktree add .claude/worktrees/base <merge-base>``, copy
``tests/_support/`` and ``scripts/capture_off_golden.py``), run ``--check`` against the OLD fixture. With an
unchanged scenario it must report NO differing turn: that is the proof that the machinery reproduces the old
fixture byte for byte. With a changed scenario it must report exactly the turns the scenario edit moves.

To re-capture AT the change that makes the fixture move (the usual case): commit the ``primer/``
change, run the script from that checkout with ``--expect-changed ... --reason ...``, and put the new
fixture in the SAME commit (``git commit --amend``; the tree hash of ``primer/`` does not change when
``tests/`` does).

To reproduce an OLD fixture (proof that it described its pinned code): check out the pinned commit,
copy ``tests/_support/off_golden.py`` and this script from the branch if they do not exist there,
and run it; the output must equal the committed fixture byte for byte except ``captured_from``.

    git worktree add .claude/worktrees/pin <sha> && cd .claude/worktrees/pin
    python scripts/capture_off_golden.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Run as a script, sys.path[0] is scripts/, which has its own ``tests`` package and would shadow the
# repository's: drop it, and put the repository root first.
sys.path[:] = [str(ROOT), *(p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent)]

from tests._support.golden_compare import behaviour, changed_turns  # noqa: E402
from tests._support.off_golden import run_scenario  # noqa: E402

FIXTURE = ROOT / "tests" / "agent" / "fixtures" / "off_mode_golden.json"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def _parse_turns(text: str | None) -> list[int]:
    if not text:
        return []
    try:
        return sorted({int(part) for part in text.split(",") if part.strip()})
    except ValueError:
        raise SystemExit(f"--expect-changed takes 1-based turn numbers like 2,3, got {text!r}") from None


def _report(changed: dict[int, list[str]], *, limit: int = 8) -> str:
    lines = []
    for turn, paths in sorted(changed.items()):
        lines.append(f"  turn {turn}: {len(paths)} differing path(s)")
        lines += [f"    {path}" for path in paths[:limit]]
        if len(paths) > limit:
            lines.append(f"    ... and {len(paths) - limit} more")
    return "\n".join(lines)


def decide(
    *, old: dict | None, new: dict, declared: list[int], reason: str | None, init: bool,
) -> tuple[str, dict[int, list[str]]]:
    """What a capture may do: ``("write-new" | "write-metadata" | "refuse:<why>", changed turns)``.

    Pure so it can be tested without running the scenario."""
    if old is None:
        return ("write-new", {}) if init else ("refuse:there is no fixture to compare with; pass --init for a first capture", {})
    changed = changed_turns(new, old)
    if not changed:
        if declared:
            return f"refuse:turn(s) {declared} were declared as changed and none changed", changed
        return "write-metadata", changed
    undeclared = sorted(t for t in changed if t not in declared)
    if undeclared:
        return f"refuse:turn(s) {undeclared} changed and were not declared with --expect-changed", changed
    unchanged = sorted(t for t in declared if t not in changed)
    if unchanged:
        return f"refuse:turn(s) {unchanged} were declared as changed and did not change", changed
    if not reason:
        return "refuse:declared changes need --reason", changed
    return "write-new", changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--allow-dirty", action="store_true", help="capture even if primer/ has uncommitted changes")
    parser.add_argument("--expect-changed", help="1-based turns allowed (and required) to change, e.g. 2,3")
    parser.add_argument("--reason", help="why the declared turns change (recorded on the fixture)")
    parser.add_argument("--init", action="store_true", help="first capture: there is no fixture to compare with")
    parser.add_argument("--check", action="store_true", help="never write: report which turns differ and exit non-zero unless they are the declared ones")
    args = parser.parse_args(argv)
    declared = _parse_turns(args.expect_changed)
    # The primer/ TREE hash, not the commit: rebase-merge orphans commit SHAs, while the tree of primer/ is
    # the same object after a rebase, a squash or a cherry-pick of the commit that produced it.
    tree = _git("rev-parse", "HEAD:primer")
    dirty = _git("status", "--porcelain", "--", "primer")
    if dirty and not args.allow_dirty and not args.check:
        print(f"refusing to capture: primer/ has uncommitted changes, so the fixture would not describe tree {tree}:\n{dirty}",
              file=sys.stderr)
        return 1
    result = asyncio.run(run_scenario())
    old_text = FIXTURE.read_text() if FIXTURE.exists() else None
    old = json.loads(old_text) if old_text else None
    decision, changed = decide(old=old, new=result, declared=declared, reason=args.reason, init=args.init)
    if args.check:
        ok = sorted(changed) == declared
        print(f"turns that differ from the committed fixture: {sorted(changed) or 'none'} (declared: {declared or 'none'})")
        if changed:
            print(_report(changed))
        return 0 if ok else 1
    if decision.startswith("refuse:"):
        print(f"refusing to write the fixture: {decision[len('refuse:'):]}", file=sys.stderr)
        if changed:
            print(_report(changed), file=sys.stderr)
        return 1
    recaptures = list((old or {}).get("recaptures", []))
    if decision == "write-new" and old is not None:
        recaptures.append({
            "previous_fixture_sha256": hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
            "previous_captured_from": old["captured_from"],
            "changed_turns": declared,
            "reason": args.reason,
        })
    body = behaviour(old) if decision == "write-metadata" else behaviour(result)
    fixture = {"captured_from": tree, **({"recaptures": recaptures} if recaptures else {}), **body}
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    shown = FIXTURE.relative_to(ROOT) if FIXTURE.is_relative_to(ROOT) else FIXTURE
    print(f"wrote {shown} ({decision}) from primer/ tree {tree}: "
          f"{body['call_count']} LLM calls, {len(body['turns'])} turns; changed turns: {sorted(changed) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
