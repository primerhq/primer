"""A counter that returns the heuristic as a count must never exist, wired or not.

``count_prompt_tokens`` was groundwork first: before the adapter-hardening slice the
Anthropic, Gemini and HF counters swallowed their own errors and returned the
character heuristic as a count, and wiring the wrapper to a turn then would have made
a decision on a number nobody vouches for. That slice has landed, so the check no
longer waits for a wiring import: it holds unconditionally, and a counter that starts
swallowing again fails here at once, not only after something imports the wrapper.
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


def test_no_vendor_counter_returns_the_heuristic_as_a_count():
    still_swallowing = [
        name for name in _SWALLOWERS
        if _returns_the_heuristic_as_a_count(
            (ROOT / "primer" / "llm" / "_tokenizer" / name).read_text()
        )
    ]
    wired = _wired()
    assert not still_swallowing, (
        f"{still_swallowing} return the character heuristic as a count; a counter "
        "must raise (or return a TokenCount that names what it estimated) instead. "
        f"Modules wiring primer.llm.counting right now: {wired or 'none'}"
    )


def test_the_swallowing_scanner_flags_a_return_but_not_an_added_estimate():
    swallow = "def f():\n    try:\n        return real()\n    except Exception:\n        return count_tokens_char_fallback(messages=[])\n"
    nested = "def f(tok):\n    if tok is None:\n        return 1 + count_tokens_char_fallback(messages=[])\n"
    adds_estimate = "def f():\n    total = real()\n    total += count_tokens_char_fallback(messages=[])\n    return total\n"
    assert _returns_the_heuristic_as_a_count(swallow)
    assert _returns_the_heuristic_as_a_count(nested)
    assert not _returns_the_heuristic_as_a_count(adds_estimate)


def test_the_gate_sees_a_wiring_import_and_ignores_prose(tmp_path):
    """The scanner finds a real import in each spelling and not a docstring
    mention, so the wiring list the failure message prints is neither empty by
    accident nor polluted by prose."""
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


def test_the_real_counters_are_scanned_and_clean():
    """Pins the scanner against the real files so the unconditional check above
    cannot pass vacuously: it reads all three, and they exist."""
    for name in _SWALLOWERS:
        path = ROOT / "primer" / "llm" / "_tokenizer" / name
        assert path.is_file(), name
        assert "def count_tokens_" in path.read_text(), f"{name} no longer defines a counter"
