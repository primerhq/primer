"""Count the form labels that name nothing: ``<label className="field-label">`` with no ``htmlFor``.

A bare ``field-label`` is a SIBLING of its input, so ``input.labels`` is empty, clicking the visible text focuses nothing and a screen reader lands on an unnamed edit field
(console review C-003). ``FormField`` (``ui/components/shared/form-field.jsx``) ties the label to its control; a form row uses it instead of a hand-drawn label.

The count is a RATCHET: ``tests/ui/field_label_baseline.json`` records the number per file and ``tests/ui/test_field_label_ratchet.py`` fails when a file has more than its
baseline (a new bare label) or fewer (the baseline must go down with it, so the gain cannot be given back later).

Run as a script: ``uv run python scripts/audit_field_labels.py`` prints the counts; ``--write`` rewrites the baseline from the tree.
Or import in tests and call ``scan()``."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "ui"
BASELINE = REPO / "tests" / "ui" / "field_label_baseline.json"
SKIPPED_DIRS = ("vendor",)


def _tag_end(src: str, start: int) -> int:
    """Index just past the ``>`` that closes the opening tag starting at ``start``: a ``>`` inside a ``{...}`` expression or a quoted attribute value does not close it."""
    depth = 0
    quote = ""
    i = start
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth <= 0:
            return i + 1
        i += 1
    return len(src)


_NATIVE_CONTROL = re.compile(r"<(input|select|textarea)\b")
_COMMENT_BLOCK = re.compile(r"^[ \t]*/\*.*?\*/", re.S | re.M)
_COMMENT_LINE = re.compile(r"^[ \t]*//.*$", re.M)


def _without_comment_lines(src: str) -> str:
    """Drop the comments that start a line (a comment may quote the very pattern this counts). A comment after code on the same line stays: telling it from a string or JSX text needs a parser."""
    return _COMMENT_LINE.sub("", _COMMENT_BLOCK.sub("", src))


def count_bare_labels(src: str) -> int:
    """The ``<label ... field-label ...>`` tags with no ``htmlFor`` that do not wrap a native control (a label around its checkbox names it implicitly)."""
    src = _without_comment_lines(src)
    count = 0
    pos = 0
    while True:
        at = src.find("<label", pos)
        if at < 0:
            return count
        end = _tag_end(src, at + len("<label"))
        tag = src[at:end]
        pos = end
        if src[at + len("<label")] not in " \t\r\n" or "field-label" not in tag or "htmlFor" in tag:
            continue
        close = src.find("</label>", end)
        if not _NATIVE_CONTROL.search(src[end:close if close >= 0 else len(src)]):
            count += 1


def scan() -> dict[str, int]:
    """Bare labels per file (only files that have any), keyed by the posix path under ``ui/``."""
    out: dict[str, int] = {}
    for path in sorted(UI.rglob("*.jsx")):
        rel = path.relative_to(UI)
        if rel.parts[0] in SKIPPED_DIRS:
            continue
        n = count_bare_labels(path.read_text(encoding="utf-8"))
        if n:
            out[rel.as_posix()] = n
    return out


def load_baseline() -> dict[str, int]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def render(counts: dict[str, int]) -> str:
    return json.dumps(counts, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    counts = scan()
    if "--write" in argv:
        BASELINE.write_text(render(counts), encoding="utf-8")
        print(f"wrote {BASELINE.relative_to(REPO)}: {sum(counts.values())} bare labels in {len(counts)} files")
        return 0
    for rel, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"{n:4d}  {rel}")
    print(f"{sum(counts.values()):4d}  total in {len(counts)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
