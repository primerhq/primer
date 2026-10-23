"""An INSTALL or SYNC enqueued the way the routes and the harness tools enqueue one: stamped with the inputs it is asked for.

The worker refuses an install or sync whose row carries no record of its inputs, or whose inputs changed since it was enqueued (ticket
01a16a29, ``primer.harness.dispatch._refuse_changed_inputs``). A test that sets ``pending_operation`` by hand, or hands a row straight to
``_do_install`` / ``_do_sync``, stamps it with these helpers, as ``POST /v1/harnesses/{id}/install`` and ``harness__install`` do.
"""

from __future__ import annotations

from typing import Any

from primer.harness.hashes import operation_input_digests
from primer.model.harness import Harness, HarnessOperation


def stamped(row: Harness, **changes: Any) -> Harness:
    """``row`` with ``changes`` applied and the digests of its inputs stamped as they then stand."""
    changed = row.model_copy(update=changes) if changes else row
    return changed.model_copy(update={"operation_inputs": operation_input_digests(changed)})


def enqueued(row: Harness, operation: HarnessOperation, **changes: Any) -> Harness:
    """``row`` with ``changes`` applied, ``operation`` pending, and the inputs it is asked for stamped."""
    return stamped(row, **changes, pending_operation=operation)


__all__ = ["enqueued", "stamped"]
