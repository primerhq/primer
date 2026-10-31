"""No doc, comment or docstring still states the reversed G7-02 shared-read rule (access-control Phase 6, Task 4).

The rule "sessions and their transcripts are a shared read for every signed-in user" was reversed in access-control Phase 2: a
session and its transcript are read through the session's workspace's access. A statement of the old rule in a doc or a code
comment misleads the reader (and an agent that reads the docs), so the old wording fails here, anywhere under ``docs/`` or
``primer/``. The one place that names the old decision is the historical note in the access-control architecture doc, and only
as the thing that was reversed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_CLAIMS = re.compile(r"shared read|G7-02 decision", re.IGNORECASE)

# The historical note that records the reversal (the access-control architecture doc): the only place that names the old
# decision, and only as the thing that was reversed.
_ALLOWED = (
    ("docs/dev/architecture/access-control.md", "was reversed in access-control Phase 2"),
)


def _claim_lines() -> list[str]:
    found: list[str] = []
    for sub in ("docs", "primer"):
        for path in sorted((ROOT / sub).rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".py"}:
                continue
            rel = path.relative_to(ROOT).as_posix()
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not _CLAIMS.search(line):
                    continue
                if any(rel == allowed_rel and phrase in line for allowed_rel, phrase in _ALLOWED):
                    continue
                found.append(f"{rel}:{i}: {line.strip()[:120]}")
    return found


def test_no_doc_or_comment_still_states_the_shared_read_rule():
    assert _claim_lines() == []
