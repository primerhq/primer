"""Which tool_wait batches a park references: the one membership test over a ``parked_state`` blob.

Phase 3 stage 7a (plan 3.3, the lead's ruling C1). :func:`batches_referenced_by_park` is THE answer to "which
batches does this park hold, and which task ids are in them": the executor's liveness check, the wake hook, the
resolver, the reconciler and the timeout step are to ask it (none of them exists yet, so it has no caller today).
The modules that read the blob's batch keys themselves on main are a frozen allowlist in a hygiene test
(``tests/session/test_tool_wait_batch_readers.py``) that fails any new reader; later PRs shrink the list as they
move those readers here.

A BATCH is one group of tool-call tasks parked together:

* a graph park holds one batch per node, as the entries of ``graph_checkpoint['pending_tool_waits']``. That is true
  of the classic ``ParkedState`` blob of a MIXED park (a human gate beside the batches; it has no top-level
  ``kind``) and of the pure tool_wait blob's own ``graph_checkpoint``. An entry stores ``outstanding_task_ids`` and
  ``notifying_results`` (``[id, result]`` pairs, the id first).
* the agent surface holds ONE batch: the top-level ``outstanding_task_ids`` / ``notifying_task_ids`` of a tool_wait
  blob (``kind == "tool_wait"``) that has NO ``graph_checkpoint``.

A pure graph tool_wait blob ALSO carries top-level lists, flattened across its nodes (``ToolWaitPark`` is flattened
by the graph executor). Whenever a ``graph_checkpoint`` is present they are a projection of its entries, not a batch,
and are ignored: read as one, "the batch's first id" would give one node's key to every node's tasks.

The key of a batch is its first STORED id, ``outstanding[0]``, else ``notifying[0]``, exactly as the blob holds it
(session-qualified since S1b, bare in a park written before that). The helper is pure and parser-free: it never
splits an id (the membership question needs no session id), so a caller that wants a wake key derives it from the
key it got here. It never raises on a malformed blob: a value of the wrong type contributes nothing, so a broken
entry hides neither its well-formed siblings nor the rest of the park.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The blob's own discriminator (``primer.worker.yield_runtime.TOOL_WAIT_PARKED_STATE_KIND``), spelled here so this
# module imports nothing and the claim adapters can use it; the tests build their blobs with ``ToolWaitParkedState``
# itself, so the two cannot drift apart silently.
_TOOL_WAIT_KIND = "tool_wait"


@dataclass(frozen=True)
class BatchRef:
    """One batch a park references.

    ``node_id`` is the graph node of a graph park's entry and ``None`` for the agent surface (and for a graph entry
    whose stored node id is not a string). ``outstanding_ids`` and ``notifying_ids`` are the batch's task ids in
    stored order and stored form; together they are every task the batch waits on.
    """

    node_id: str | None
    outstanding_ids: tuple[str, ...]
    notifying_ids: tuple[str, ...]


def _ids(value: Any) -> tuple[str, ...]:
    """The string ids of a stored id list; anything else contributes nothing."""
    if not isinstance(value, list | tuple):
        return ()
    return tuple(i for i in value if isinstance(i, str))


def _notifying_ids(value: Any) -> tuple[str, ...]:
    """The ids of a graph entry's ``notifying_results`` (``[id, result]`` pairs; a JSON round trip makes them lists)."""
    if not isinstance(value, list | tuple):
        return ()
    return tuple(
        pair[0] for pair in value
        if isinstance(pair, list | tuple) and pair and isinstance(pair[0], str)
    )


def _add(batches: dict[str, BatchRef], ref: BatchRef) -> None:
    ids = ref.outstanding_ids or ref.notifying_ids
    if ids:
        # Ids are unique within a park, so two batches never share a first id in a well-formed blob; if a broken one
        # does, the first batch stored keeps the key.
        batches.setdefault(ids[0], ref)


def batches_referenced_by_park(blob: Any) -> dict[str, BatchRef]:
    """Every batch ``blob`` (a session's ``parked_state``) references, keyed on the batch's first stored id.

    ``{}`` for no park (``None``), a park that holds no tool_wait batch (a classic agent park, a graph park with no
    co-pending batch), a foreign blob, or a blob with no well-formed batch; a partly malformed blob gives its
    well-formed batches. See the module docstring for the shapes and the key.
    """
    batches: dict[str, BatchRef] = {}
    if not isinstance(blob, dict):
        return batches
    kind = blob.get("kind")
    if kind is not None and kind != _TOOL_WAIT_KIND:
        return batches
    checkpoint = blob.get("graph_checkpoint")
    if checkpoint is not None:
        # A graph park: the entries are the batches, and any top-level lists are their flattened projection.
        entries = checkpoint.get("pending_tool_waits") if isinstance(checkpoint, dict) else None
        if not isinstance(entries, list):
            return batches
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            node_id = entry.get("node_id")
            _add(batches, BatchRef(
                node_id=node_id if isinstance(node_id, str) else None,
                outstanding_ids=_ids(entry.get("outstanding_task_ids")),
                notifying_ids=_notifying_ids(entry.get("notifying_results")),
            ))
        return batches
    if kind == _TOOL_WAIT_KIND:
        _add(batches, BatchRef(
            node_id=None,
            outstanding_ids=_ids(blob.get("outstanding_task_ids")),
            notifying_ids=_ids(blob.get("notifying_task_ids")),
        ))
    return batches


__all__ = ["BatchRef", "batches_referenced_by_park"]
