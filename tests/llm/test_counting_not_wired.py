"""The counter wrapper is not on any production path yet, and may not be until S2.

``count_prompt_tokens`` is groundwork. Until the adapter-hardening slice lands, the
Anthropic, Gemini and HF counters still swallow their own errors and return the
character heuristic as a count; wiring the wrapper to a turn before that would make
a decision on a number nobody vouches for. This test is the gate: it passes while
nothing outside primer/llm imports the wrapper, and once something does, it demands
that the swallowing counters are gone.

When a wiring slice lands it updates this test deliberately (that is the point).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SWALLOWERS = ("anthropic.py", "gemini.py", "hf.py")


def _imports_counting(source: str) -> bool:
    """A real import of the wrapper (prose in a docstring does not count)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module == "primer.llm.counting":
                return True
            if node.module == "primer.llm" and any(a.name == "counting" for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(a.name == "primer.llm.counting" for a in node.names):
            return True
    return False


def _wired(root: Path = ROOT) -> list[str]:
    wired = []
    for path in sorted((root / "primer").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("primer/llm/"):
            continue
        if _imports_counting(path.read_text()):
            wired.append(rel)
    return wired


def test_wiring_the_wrapper_requires_counters_that_raise():
    wired = _wired()
    if not wired:
        return
    still_swallowing = [
        name for name in _SWALLOWERS
        if "count_tokens_char_fallback"
        in (ROOT / "primer" / "llm" / "_tokenizer" / name).read_text()
    ]
    assert not still_swallowing, (
        f"{wired} wire primer.llm.counting while {still_swallowing} still return the "
        "character heuristic as a count; land the adapter-hardening slice (S2) first"
    )


def test_the_gate_sees_a_wiring_import_and_ignores_prose(tmp_path):
    """The scanner finds a real import in each spelling and not a docstring
    mention, so the gate can neither pass vacuously nor fail on prose."""
    pkg = tmp_path / "primer" / "agent"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("from primer.llm.counting import count_prompt_tokens\n")
    (pkg / "b.py").write_text("import primer.llm.counting\n")
    (pkg / "c.py").write_text("from primer.llm import counting\n")
    (pkg / "prose.py").write_text('"""see primer.llm.counting.count_prompt_tokens"""\n')
    (tmp_path / "primer" / "llm").mkdir()
    (tmp_path / "primer" / "llm" / "inside.py").write_text("from primer.llm.counting import x\n")
    assert _wired(tmp_path) == [
        "primer/agent/a.py", "primer/agent/b.py", "primer/agent/c.py",
    ]
