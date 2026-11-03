"""Join a session's log segments back into ``messages.jsonl`` (the rollback path of the segmented session log).

A session whose log has rotated holds ``messages.jsonl`` (segment 0) and ``messages.0001.jsonl``, ``messages.0002.jsonl`` ... A build that predates segmentation reads only
``messages.jsonl``, so rolling back needs the single file again. This script concatenates the segments, in order, into ``messages.jsonl`` and removes the others.

Safety: it refuses a session whose ``session.json`` says it is running (a turn may be writing), and a log whose segment numbers have a gap (the bytes of the missing
file cannot be invented). The joined file is written to a temporary file and moved over ``messages.jsonl`` atomically, and a marker naming the segments is written before
that move, so a run that dies half way is completed by the next run instead of joining the same bytes twice. Nothing is staged in git: the operator commits.

    uv run python scripts/join_session_log.py --state <workspace>/.state --session <id> [--dry-run]
    uv run python scripts/join_session_log.py --state <workspace>/.state --all [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from primer.workspace.segments import LOG_NAME, SegmentGap, ordered

#: Written (atomically) before the joined file replaces ``messages.jsonl``; read by the next run to finish the unlinks.
MARKER = "messages.join.json"
_TMP = "messages.join.tmp"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _finish_interrupted(slot: Path) -> str | None:
    """A previous run died: finish it when its joined file is in place, else clean up after it. Returns what it did, or None when there was nothing."""
    marker = slot / MARKER
    if not marker.exists():
        return None
    plan = json.loads(marker.read_text())
    log = slot / LOG_NAME
    if log.exists() and log.stat().st_size == plan["joined_size"]:
        for name in plan["segments"]:
            (slot / name).unlink(missing_ok=True)
        marker.unlink()
        return "completed an interrupted join"
    (slot / _TMP).unlink(missing_ok=True)
    marker.unlink()
    return None


def join_session(slot: Path, *, dry_run: bool = False) -> str:
    """Join one session. Returns a one-line report; raises ``SegmentGap`` or ``RuntimeError`` for a session that must not be touched."""
    if not dry_run:
        done = _finish_interrupted(slot)
        if done:
            return done
    found = ordered(p.name for p in slot.glob("messages*.jsonl"))
    if [n for n, _ in found] != list(range(len(found))):
        raise SegmentGap(f"segments {[n for n, _ in found]} are not 0..{len(found) - 1}: a gap, nothing was joined")
    if len(found) <= 1:
        return "nothing to join"
    info = slot / "session.json"
    if info.exists() and json.loads(info.read_text()).get("status") == "running":
        raise RuntimeError("the session is running: a turn may be writing its log; end or pause it first")
    segments = [name for _, name in found]
    if dry_run:
        return f"would join {len(segments)} segments ({', '.join(segments)}) into {LOG_NAME}"
    joined = b"".join((slot / name).read_bytes() for name in segments)
    _write_atomic(slot / _TMP, joined)
    _write_atomic(marker := slot / MARKER, json.dumps({"segments": segments[1:], "joined_size": len(joined)}).encode())
    os.replace(slot / _TMP, slot / LOG_NAME)
    for name in segments[1:]:
        (slot / name).unlink()
    marker.unlink()
    return f"joined {len(segments)} segments into {LOG_NAME} ({len(joined)} bytes)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Join a session's log segments back into messages.jsonl (rollback of the segmented log).")
    parser.add_argument("--state", required=True, type=Path, help="the workspace's .state directory")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--session", help="one session id")
    which.add_argument("--all", action="store_true", help="every session under sessions/")
    parser.add_argument("--dry-run", action="store_true", help="say what would be joined; change nothing")
    args = parser.parse_args(argv)

    sessions = args.state / "sessions"
    slots = sorted(p for p in sessions.iterdir() if p.is_dir()) if args.all and sessions.is_dir() else [sessions / args.session] if args.session else []
    failed = 0
    for slot in slots:
        if not slot.is_dir():
            print(f"{slot.name}: no such session", file=sys.stderr)
            failed += 1
            continue
        try:
            print(f"{slot.name}: {join_session(slot, dry_run=args.dry_run)}")
        except (SegmentGap, RuntimeError) as exc:
            print(f"{slot.name}: REFUSED: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
