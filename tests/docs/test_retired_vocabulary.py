"""Fail when a repo doc still describes something v2 no longer has.

Mirrors primerhq.github.io/tests/test_retired_vocabulary.py, applied to
this repo's doc set. S9 section 6: verify no doc still describes chats,
mounting, docling, or the old shells.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Patterns are matched case-SENSITIVELY, exactly like the website copy, so
# every entry that can appear capitalised (prose, headings, product names)
# spells both cases out. `docling` really does appear as "Docling" in
# README prose today, and `studio2` as "studioV2" in the studio docs.
#
# `switch_to_agent` is NOT here, though the plan listed it. The tool was
# not renamed: S1 added switch_binding beside it, and S9 Task 11 ported
# switch_to_agent onto the same seam rather than retiring it, because the
# capability it names survives. A doc describing it describes something
# the product HAS.
RETIRED: dict[str, str] = {
    r"\bchat_id\b": "chats were removed in S1; sessions carry the workstream",
    r"\bPOST /v1/chats\b": "the chats router was deleted in S1",
    r"\bchats\.jsx\b": "the chat page was deleted in S1",
    r"\b[Mm]ount(ed|ing)? (a )?collection\b":
        "collection mounting was removed by S1 P5",
    r"\b[Dd]ocling\b": "the docling ingestion path was removed in S2",
    r"\b[Ss]tudio2\b|\bstudioV2\b":
        "the studio2 trial was deleted at the S8 flag day",
    r"\b[Cc]lassic console\b":
        "the classic console was deleted at the S8 flag day",
}

EXEMPT = {
    "docs/ux-revamp.md",          # its job is to describe the change
    "docs/dev/vision",            # historical narrative, deliberately dated
}


def _docs() -> list[Path]:
    out: list[Path] = [REPO / "README.md", REPO / "AGENTS.md"]
    for root in ("docs/dev", "docs/agents"):
        out.extend((REPO / root).rglob("*.md"))
    return sorted(
        p for p in out
        if p.exists()
        and not any(str(p.relative_to(REPO)).startswith(e) for e in EXEMPT)
    )


def test_no_doc_uses_retired_vocabulary() -> None:
    hits: list[str] = []
    for doc in _docs():
        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(REPO)
        for pattern, why in RETIRED.items():
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                hits.append(f"{rel}:{line}: {match.group(0)!r} - {why}")
    assert not hits, "docs still use retired vocabulary:\n  " + "\n  ".join(hits)


def test_every_pattern_matches_its_sample() -> None:
    """A guard whose pattern matches nothing is decoration."""
    samples = {
        r"\bchat_id\b": "the envelope carries chat_id",
        r"\bPOST /v1/chats\b": "call POST /v1/chats to start one",
        r"\bchats\.jsx\b": "the page lives in chats.jsx",
        r"\b[Mm]ount(ed|ing)? (a )?collection\b": "Mounting a collection is optional",
        r"\b[Dd]ocling\b": "install the Docling extra",
        r"\b[Ss]tudio2\b|\bstudioV2\b": "the studioV2 shell replaced Studio2",
        r"\b[Cc]lassic console\b": "the Classic console still redirects",
    }
    assert set(samples) == set(RETIRED), (
        "every RETIRED pattern needs a sample proving it matches"
    )
    for pattern, sample in samples.items():
        assert re.search(pattern, sample), f"pattern never matches: {pattern}"
