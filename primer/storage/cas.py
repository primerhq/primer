"""``patch_if`` with a drift tripwire.

A rejected ``patch_if`` is normally a lost race: the row changed since the caller read it. There is
one rejection that is NOT a race and must never be silent: the DATABASE refuses a guard that Python
says the row satisfies, because what the backend compares differs from what ``dump_for_storage`` (and so
:func:`primer.storage.raw_generation`) produces (a serializer change, a value the backend normalizes).
The write is then refused forever, and every caller that treats ``None`` as "someone else won" stalls
without a sound.

:func:`patch_if_checked` closes that: after a rejection it re-reads the row and evaluates the SAME
``where`` against the fresh document in Python. If the fresh row still satisfies it, the database's
refusal was not a race; it logs ERROR and increments ``storage_cas_drift_total{model}``. The return value
is unchanged (``None``), so a caller's handling does not move. A real race can raise a false alarm: one
concurrent write that moves the row INTO the allowed set between the rejected UPDATE and the re-read is enough
(``parked`` -> ``running`` against ``where status in ["running"]``). An alarm is the right failure mode for this
check, and it only catches over-rejection (a refusal Python disagrees with), never a backend that applies
when Python says it should not. It cannot catch a caller that hand-builds a wrong comparison value, because
Python then disagrees too; that is why callers take the value from ``raw_generation``, which the contract
tests pin as a no-op round trip. The log names the guarded FIELDS only: a guard on a secret field would
otherwise print the plaintext (``raw_generation`` unmasks SecretStr).

Put the ENDED (or any "no longer eligible") exclusion in ``where`` itself, as a list of the allowed
statuses: a row that legitimately left eligibility then fails the Python check too and stays quiet.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import primer.observability.metrics as _metrics
from primer.int.storage import Storage
from primer.model.common import dump_for_storage
from primer.storage._patch import WhereKey, document_matches

logger = logging.getLogger(__name__)


async def patch_if_checked(
    storage: Storage[Any],
    id: str,  # noqa: A002
    patch: Mapping[str, Any] | None = None,
    *,
    where: Mapping[WhereKey, Sequence[Any]],
    set_paths: Mapping[tuple[str, ...], Any] | None = None,
    conn: Any | None = None,
) -> Any | None:
    """:meth:`Storage.patch_if`, plus the drift tripwire on a rejection."""
    result = await storage.patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)
    if result is not None:
        return result
    fresh = await storage.get(id, conn=conn)
    if fresh is not None and document_matches(dump_for_storage(fresh), where):
        model = type(fresh).__name__
        _metrics.storage_cas_drift_total.labels(model).inc()
        logger.error(
            "patch_if on %s %r was rejected although a fresh read still satisfies the guard: "
            "the comparison value does not match what the backend stores (serialization drift), "
            "so this write can never apply. guarded fields=%s",
            model, id, sorted(where, key=str),
        )
    return None
