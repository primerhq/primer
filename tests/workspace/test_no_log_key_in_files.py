"""Seglog Task 9 pin: no production code writes the session log through ``files={"messages.jsonl": ...}``.

Once a log has rotated, the log key in ``files`` would clobber segment 0 with content built from a read of segment 0 alone, so the state repos refuse it. A writer that
cannot append reads the active segment (``primer.workspace.segments.read_active_segment``) and passes ``log_rewrite=LogRewrite(...)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _offenders() -> list[str]:
    out: list[str] = []
    for path in sorted((ROOT / "primer").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "messages.jsonl" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "files" and isinstance(kw.value, ast.Dict):
                    if any(isinstance(k, ast.Constant) and k.value == "messages.jsonl" for k in kw.value.keys):
                        out.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return out


def test_no_production_code_passes_the_log_key_in_files():
    assert _offenders() == []
