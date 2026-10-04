"""A counter that returns the heuristic as a count must never sit under a wired wrapper.

``count_prompt_tokens`` was groundwork first: before the adapter-hardening slice the
Anthropic, Gemini and HF counters swallowed their own errors and returned the
character heuristic as a count, and wiring the wrapper to a turn then would have made
a decision on a number nobody vouches for. That slice has landed; this test keeps it
true. It passes while nothing outside primer/llm imports the wrapper, and once
something does it demands that none of the three counters swallows.
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


def _returns_the_heuristic_as_a_count(source: str) -> bool:
    """True if some ``return`` hands back ``count_tokens_char_fallback(...)``.

    That is the swallowing pattern: a counter that catches its own failure and
    returns the heuristic as if it were a count. Using the heuristic to ADD an
    estimated component to a real count (Gemini's system and tool estimates) is a
    different thing and is not flagged.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Return) and node.value is not None:
            for sub in ast.walk(node.value):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "count_tokens_char_fallback"
                ):
                    return True
    return False


def test_wiring_the_wrapper_requires_counters_that_raise():
    wired = _wired()
    if not wired:
        return
    still_swallowing = [
        name for name in _SWALLOWERS
        if _returns_the_heuristic_as_a_count(
            (ROOT / "primer" / "llm" / "_tokenizer" / name).read_text()
        )
    ]
    assert not still_swallowing, (
        f"{wired} wire primer.llm.counting while {still_swallowing} still return the "
        "character heuristic as a count; land the adapter-hardening slice (S2) first"
    )


def test_the_swallowing_scanner_flags_a_return_but_not_an_added_estimate():
    swallow = "def f():\n    try:\n        return real()\n    except Exception:\n        return count_tokens_char_fallback(messages=[])\n"
    nested = "def f(tok):\n    if tok is None:\n        return 1 + count_tokens_char_fallback(messages=[])\n"
    adds_estimate = "def f():\n    total = real()\n    total += count_tokens_char_fallback(messages=[])\n    return total\n"
    assert _returns_the_heuristic_as_a_count(swallow)
    assert _returns_the_heuristic_as_a_count(nested)
    assert not _returns_the_heuristic_as_a_count(adds_estimate)
