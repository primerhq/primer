"""The canvas subsystem doc and the wiki canvas page (pivot P5, Task 24).

Pins that the dev doc names the canvas contract (its record kinds, the platform event
types, the frame's CSP and sandbox, the blocks the agent reads, the wait tool and the
default media policy) and that the wiki feature page documents the three canvas tools.
RED until ``docs/dev/subsystems/canvas.md`` and ``docs/wiki/human-in-the-loop/canvas.md``
exist; the docs hygiene suite is also red while ``canvas`` is in ``SUBSYSTEM_DOCS`` but the
doc is missing.
"""

from __future__ import annotations

from pathlib import Path

from tests.docs._wiki import repo_wiki

REPO = Path(__file__).resolve().parents[2]
DOCS_DEV = REPO / "docs" / "dev"


def test_the_canvas_doc_names_the_contract() -> None:
    text = (DOCS_DEV / "subsystems" / "canvas.md").read_text(encoding="utf-8")
    for needle in (
        "canvas_op", "canvas_event", "canvas.updated", "canvas.event",
        "allow-scripts allow-forms", "frame-ancestors 'self'",
        "<canvas_event", "<canvas_changes", "canvas_wait", "any-https",
    ):
        assert needle in text, needle


def test_the_wiki_page_documents_the_three_tools() -> None:
    page = repo_wiki().pages["human-in-the-loop/canvas"]
    assert page.meta["tools"] == [
        "canvas__canvas_get", "canvas__canvas_apply", "canvas__canvas_wait",
    ]
