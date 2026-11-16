"""Internal ownership and ordering contract for opt-in model transactions."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps

from primer.model.common import Identifiable
from primer.model.except_ import ConfigError

locking_context: ContextVar[LockingTransaction | None] = ContextVar("storage_locking_transaction", default=None)


def current_transaction():
    transaction = locking_context.get()
    if transaction is not None and transaction.owner is not asyncio.current_task():
        raise ConfigError("inherited locking transaction belongs to another task")
    return transaction


def prepared_models(models):
    if not isinstance(models, tuple) or not models or any(
        not isinstance(model, type) or not issubclass(model, Identifiable) for model in models
    ):
        raise ConfigError("locking_transaction requires a nonempty tuple of model classes")
    if len(set(models)) != len(models):
        raise ConfigError("locking_transaction models must be distinct")
    return tuple(sorted(models, key=lambda model: (model.__module__, model.__qualname__)))


class LockingTransaction:
    """Opaque conn token; not a SQL connection, authorization or gap lock."""

    def __init__(self, provider, connection, models):
        self.provider = provider
        self.connection = connection
        self.models = frozenset(models)
        self.owner = asyncio.current_task()
        self.active = True
        self.failed = False
        self._keys: set[tuple[str, str]] = set()
        self._last_key: tuple[str, str] | None = None

    def check(self, provider, model):
        if not self.active or self.owner is not asyncio.current_task() or self.provider is not provider:
            raise ConfigError("locking transaction belongs to another task/provider or has ended")
        if model not in self.models:
            raise ConfigError("model was not prepared for this locking transaction")
        if self.failed:
            raise ConfigError("locking transaction requires rollback after a failed operation")
        return self.connection

    def check_key(self, table, entity_id):
        if not isinstance(entity_id, str):
            raise ConfigError("locking row id must be a string")
        key = (table, entity_id)
        if key in self._keys:
            return
        if self._last_key is not None and key < self._last_key:
            raise ConfigError("lock rows in ascending (storage table, id) order")
        self._last_key = key

    def hold_key(self, table, entity_id):
        self._keys.add((table, entity_id))

    def ensure_committable(self):
        if self.failed:
            raise ConfigError("locking transaction rolled back after a caught storage failure")


def require_transaction(conn, provider, model):
    if not isinstance(conn, LockingTransaction):
        raise ConfigError("get_for_update requires an active locking transaction token")
    return conn.check(provider, model)


def model_operation(fn):
    """Preserve legacy calls; validate every model operation in the opt-in unit."""
    @wraps(fn)
    async def run(self, *args, **kwargs):
        provider, model, table = self.locking_identity()
        supplied = kwargs.get("conn")
        transaction = supplied if isinstance(supplied, LockingTransaction) else current_transaction()
        if transaction is None:
            return await fn(self, *args, **kwargs)
        transaction.check(provider, model)
        if supplied is not transaction:
            raise ConfigError("thread the locking transaction token through every model operation")
        if fn.__name__ != "get":
            entity = args[0] if args else kwargs.get("entity", kwargs.get("id"))
            entity_id = entity.id if fn.__name__ in {"create", "update", "update_unless"} else entity
            transaction.check_key(table, entity_id)
        try:
            result = await fn(self, *args, **kwargs)
            held = result[0] is not None if fn.__name__ == "patch_if_or_current" else result is not None
            if fn.__name__ not in {"get", "get_for_update"} and (held or fn.__name__ == "delete"):
                transaction.hold_key(table, entity_id)
            return result
        except BaseException:
            transaction.failed = True
            raise
    return run


async def drain_cleanup(awaitable, *, propagate_cancellation=False):
    """Finish queued cleanup before releasing ownership, despite repeat cancellation.

    Cleanup runs as a child only because the cancelling task cannot safely await
    SQLite's queued worker future directly. Never use this to run model operations.
    Preserve an exception already unwinding. A clean exit instead asks to
    propagate cancellation first received here, after cleanup finishes.
    """
    cancellation = None
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    result = task.result()
    if propagate_cancellation and cancellation is not None:
        raise cancellation
    return result
