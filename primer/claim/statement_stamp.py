"""When the claim statement ran, for the caller that awaits ``claim_due``.

The lease expiry is stamped from the START of the claim statement, so a worker's staleness clock (the lease watchdog)
must start there too. The caller of ``claim_due`` can only see two later moments: before the call (it includes the wait
for a pool connection, which can be long under saturation) and after it (it includes the per-lease ``on_claim`` hooks).
The engine therefore records the moment its statement returned, after the connection wait and before the hooks, in
this context variable. The pool awaits ``claim_due`` directly in its own task, so a value set inside it is visible
after the call; an engine that does not set it leaves ``None`` and the pool falls back to its pre-call stamp.
"""

from __future__ import annotations

from contextvars import ContextVar

claim_statement_at: ContextVar[float | None] = ContextVar("claim_statement_at", default=None)
