"""Seglog ratchet: no module outside the allowed set spells the session log's file name (``messages.jsonl``) in code.

After segmentation the name ``messages.jsonl`` means "segment 0 only". A reader that opens it directly serves a prefix of a rotated log, silently. Every reader
goes through ``primer.workspace.segments.SessionLog`` instead, so this test counts, per file, the string literals (docstrings excluded) that contain the name, and
compares them with ``raw_log_path_baseline.json``: a count that goes UP (or a new file) fails, and so does a count that went DOWN without the baseline being lowered
(a seglog task that migrates a reader lowers it in the same commit). The allowed set is the files that own the layout.

Rewrite the baseline deliberately: ``PRIMER_WRITE_RAW_LOG_BASELINE=1 uv run pytest tests/workspace/test_no_raw_log_path.py``.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("raw_log_path_baseline.json")
NAME = "messages.jsonl"

#: The files that own the layout (they may spell the name freely): the segment module, the two state repos and the two workspaces' event-row appenders.
ALLOWED = frozenset({
    "primer/workspace/segments.py",
    "primer/workspace/local/state.py",
    "primer/workspace/sandbox/state.py",
    "primer/workspace/local/workspace.py",
    "primer/workspace/sandbox/workspace.py",
})


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def count_literals() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted((ROOT / "primer").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        if NAME not in text:
            continue
        tree = ast.parse(text)
        skip = _docstring_nodes(tree)
        n = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and NAME in node.value and id(node) not in skip
        )
        if n:
            counts[rel] = n
    return counts


def test_no_module_gains_a_raw_log_path():
    got = count_literals()
    if os.environ.get("PRIMER_WRITE_RAW_LOG_BASELINE"):
        BASELINE.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        return
    want = json.loads(BASELINE.read_text())
    up = {f: (want.get(f, 0), n) for f, n in got.items() if n > want.get(f, 0)}
    assert not up, f"a file spells {NAME} more than the baseline allows (baseline, now); read the log through primer.workspace.segments.SessionLog: {up}"
    down = {f: (n, got.get(f, 0)) for f, n in want.items() if got.get(f, 0) < n}
    assert not down, f"the count went down: lower tests/workspace/raw_log_path_baseline.json in the same commit (baseline, now): {down}"


def test_the_allowed_files_exist_or_are_planned():
    # segments.py is created by seglog Task 2; the others exist today.
    missing = [f for f in ALLOWED if not (ROOT / f).exists() and f != "primer/workspace/segments.py"]
    assert missing == []
