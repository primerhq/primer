"""The builder's own resource keys are in the save's invalidates (UI-L13).

The builder reads the server-side issues for its readiness chip from
``graph:status:<id>`` - the builder's own key, distinct from the detail page's
``graph-status:<id>`` (and the detail's ``graph-detail:<id>``). The save
mutation's ``invalidates`` listed the detail page's keys but not the
builder's, so after a save the chip kept the pre-save issues until the next
navigation. Same for the panel's Refresh button and the builder's post-run
refresh: their ``onRefresh`` closure refetched the panel's two keys and left
the builder's entry alone. The check runs over the source: every static
resource key the builder reads (a template-literal key at a ``useResource``
position) must be in the save's ``invalidates``, and the detail page's
``onRefresh`` must refresh the builder's key too. ``tools:catalogue`` is the
cross-surface read-only tool list (a double-quoted key the save cannot change,
and not one this test's regex sees) and the run-scoped
``graph:runstates:<id>:<runId>`` key is conditional on a run (a ternary first
argument, which the regex does not see), so neither is the save's to
invalidate.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
SRC = (UI / "components/graph-builder/graph-builder.jsx").read_text(encoding="utf-8")


def test_every_builder_resource_key_is_in_the_save_invalidates() -> None:
    m = re.search(r"const save = useMutation\([\s\S]*?invalidates:\s*\[([^\]]+)\]", SRC)
    assert m, "the save mutation lost its invalidates list"
    entries = {e.strip().strip("`\"'") for e in m.group(1).split(",")}

    reads = re.findall(r"useResource\(\s*`([^`]+)`", SRC)
    assert reads, "the builder no longer reads a template-literal resource key: the test needs updating"
    missing = [k for k in reads if k not in entries]
    assert not missing, f"the save does not invalidate the builder's own key(s): {missing}"


def test_the_builder_status_key_is_the_builder_one_not_the_detail_pages() -> None:
    # The fix must target the builder's key: the detail page's graph-status:<id>
    # was already invalidated, and adding it again would mask a missing key.
    m = re.search(r"const save = useMutation\([\s\S]*?invalidates:\s*\[([^\]]+)\]", SRC)
    assert m
    entries = [e.strip().strip("`\"'") for e in m.group(1).split(",")]
    assert "graph:status:${graphId}" in entries
    assert "graph-status:${graphId}" in entries


def test_the_detail_refresh_also_refreshes_the_builder_status_key() -> None:
    # The other half of the finding: the panel's Refresh button and the
    # builder's post-run refresh both go through the detail page's onRefresh,
    # which must reach the builder's key too (the shared cache entry, not a
    # new read of the endpoint).
    src = (UI / "components/graphs.jsx").read_text(encoding="utf-8")
    assert 'refetchKey("graph:status:" + id)' in src, (
        "the detail page's onRefresh does not refresh the builder's own status key"
    )
