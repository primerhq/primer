"""Pin the action-event emission sites by scanning the source tree.

Two directions:

* every type in ``ACTION_EVENT_TYPES`` is emitted somewhere under
  ``primer/`` - a catalog entry with no site is a broken promise;
* every ``emit("...")`` literal in the tree is in the catalog - a new
  router cannot mint an off-catalog type (the recorder would raise at
  runtime, this test catches it at review time).
"""
from __future__ import annotations

import re
from pathlib import Path

from primer.events.catalog import ACTION_EVENT_TYPES

_PRIMER = Path(__file__).resolve().parents[2] / "primer"

# Any emit-shaped call taking the type as its first (string) arg:
# recorder.emit("t"), self._event_recorder.emit("t"), and wrappers
# like _emit_document_event("t").
_EMIT_RE = re.compile(r"""emit\w*\(\s*\n?\s*["']([a-z_]+\.[a-z_]+)["']""")


def _emitted_literals() -> set[str]:
    found: set[str] = set()
    for path in _PRIMER.rglob("*.py"):
        found.update(_EMIT_RE.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_catalog_type_has_an_emitting_site():
    emitted = _emitted_literals()
    missing = sorted(ACTION_EVENT_TYPES - emitted)
    assert not missing, (
        f"catalog types with no emit site under primer/: {missing}"
    )


def test_every_emitted_literal_is_in_the_catalog():
    emitted = _emitted_literals()
    unknown = sorted(t for t in emitted if t not in ACTION_EVENT_TYPES)
    assert not unknown, (
        f"emit sites using off-catalog types: {unknown} - add them to "
        "primer/events/catalog.py"
    )
