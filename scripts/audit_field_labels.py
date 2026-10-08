"""Count the form labels that name nothing: ``<label className="field-label">`` with no ``htmlFor``, and a ``div``/``span`` only styled as one.

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
_OPEN_TAG = re.compile(r"<(label|div|span)(?=[ \t\r\n])")
# Only a double-quoted literal is read: ``className={"nv-field-label " + x}``, a template string or a variable passes unseen (a parser would be needed to see them).
_CLASS_LITERAL = re.compile(r'className="([^"]*)"')
_LABEL_CLASSES = {"field-label", "nv-field-label"}


def _without_comment_lines(src: str) -> str:
    """Drop the comments that start a line (a comment may quote the very pattern this counts). A comment after code on the same line stays: telling it from a string or JSX text needs a parser."""
    return _COMMENT_LINE.sub("", _COMMENT_BLOCK.sub("", src))


def count_bare_labels(src: str) -> int:
    """The form labels that name nothing:

    * a ``<label ... field-label ...>`` with no ``htmlFor`` that does not wrap a native control (a label around its checkbox names it implicitly);
    * a ``<div>`` or ``<span>`` whose ``className`` literal carries the ``field-label`` / ``nv-field-label`` class: an element only STYLED as a label is not one (the console
      overlays' ``NV_Field`` drew its label as a div, and every field of every overlay went unnamed). A class that merely contains the name (``field-label-row``) is another class,
      and a component handed ``labelClassName="nv-field-label"`` draws no such element itself.
    """
    src = _without_comment_lines(src)
    count = 0
    pos = 0
    while True:
        m = _OPEN_TAG.search(src, pos)
        if m is None:
            return count
        end = _tag_end(src, m.end())
        tag = src[m.start():end]
        pos = end
        if m.group(1) != "label":
            cls = _CLASS_LITERAL.search(tag)
            if cls and _LABEL_CLASSES & set(cls.group(1).split()):
                count += 1
            continue
        if "field-label" not in tag or "htmlFor" in tag:
            continue
        close = src.find("</label>", end)
        if not _NATIVE_CONTROL.search(src[end:close if close >= 0 else len(src)]):
            count += 1


def scan(root: Path | None = None) -> dict[str, int]:
    """Bare labels per file (only files that have any), keyed by the posix path under ``root`` (``ui/``; a test passes a synthetic tree)."""
    root = root or UI
    out: dict[str, int] = {}
    for path in sorted(root.rglob("*.jsx")):
        rel = path.relative_to(root)
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
