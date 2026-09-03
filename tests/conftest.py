"""Top-level shared fixtures available to all test sub-packages.

The ``fake_storage_provider`` fixture is defined here so both
``tests/api/`` and ``tests/storage/`` (and any future package) can
use it without duplication.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

import pytest

from primer.model.common import Identifiable
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import (
    CursorPage,
    CursorPageResponse,
    FieldRef,
    OffsetPage,
    OffsetPageResponse,
    Op,
    Predicate,
    Value,
)


_T = TypeVar("_T", bound=Identifiable)


class _InMemoryStorage(Generic[_T]):
    """Bare-bones in-memory ``Storage[T]`` for tests."""

    def __init__(self, model_cls: type[_T]) -> None:
        self._cls = model_cls
        self._data: dict[str, _T] = {}

    async def get(self, id: str, *, conn=None) -> _T | None:
        return self._data.get(id)

    async def create(self, entity: _T, *, conn=None) -> _T:
        if entity.id in self._data:
            raise ConflictError(f"id {entity.id!r} already exists")
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _T, *, conn=None) -> _T:
        if entity.id not in self._data:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        self._data[entity.id] = entity
        return entity

    async def update_unless(
        self, entity: _T, *, field: str, forbidden: Any, conn=None,
    ) -> _T | None:
        current = self._data.get(entity.id)
        if current is None:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        if _resolve_field(current, field) == forbidden:
            return None
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str, *, conn=None) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        # Honor order_by (additive; mirrors find) so endpoints that list +
        # order, e.g. tool_approval/records by decided_at desc, get a stable
        # ordered page as the real sqlite/postgres backends would.
        if order_by:
            for ob in reversed(order_by):
                field = ob.field
                desc = ob.direction == "desc"
                items.sort(
                    key=lambda e, f=field: (
                        _resolve_field(e, f) is None,
                        _resolve_field(e, f) if _resolve_field(e, f) is not None else 0,
                    ),
                    reverse=desc,
                )
        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor: str | None = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    async def find(self, predicate, page, *, order_by=None):
        if predicate is None:
            return await self.list(page, order_by=order_by)
        items = [e for e in self._data.values() if _eval_predicate(e, predicate)]
        # Honor order_by so DESC paginations (e.g. chat-history tail
        # fetch) get the expected last-N slice. Stable multi-key sort
        # by reversing key order. None values sort last.
        if order_by:
            for ob in reversed(order_by):
                field = ob.field
                desc = ob.direction == "desc"
                items.sort(
                    key=lambda e, f=field: (
                        _resolve_field(e, f) is None,
                        _resolve_field(e, f) if _resolve_field(e, f) is not None else 0,
                    ),
                    reverse=desc,
                )
        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor: str | None = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)


def _resolve_field(entity: Any, path: str) -> Any:
    """Walk a dotted path against a Pydantic model / dict.

    Supports ``id``, ``status``, ``binding.agent_id`` -- the patterns the
    sessions router emits. Returns ``None`` for any unresolvable segment
    so a missing field naturally fails an ``EQ`` comparison.
    """
    cur: Any = entity
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    # Coerce Enums to their string value so EQ against a Value(value=str)
    # behaves like the Postgres translator (which compares text).
    from enum import Enum
    if isinstance(cur, Enum):
        return cur.value
    return cur


def _like_match(value: str, pattern: str, *, case_insensitive: bool) -> bool:
    """Emulate SQL ``LIKE`` / ``ILIKE`` (``ESCAPE '\\'``) for the fake store.

    Translates the SQL pattern to a regex: ``%`` -> ``.*``, ``_`` -> ``.``, and
    a backslash escapes the next metacharacter to a literal. Mirrors the
    ``ESCAPE '\\'`` clause the SQL backends emit so the ``?q=`` escape test
    behaves identically over the in-memory provider.
    """
    import re

    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    flags = re.DOTALL | (re.IGNORECASE if case_insensitive else 0)
    return re.match("".join(out), value, flags) is not None


def _eval_predicate(entity: Any, node: Any) -> bool:
    """Tiny predicate evaluator for the in-memory test storage.

    Supports EQ / NE / GT / LT / GE / LE / LIKE / ILIKE / CONTAINS /
    IS_NULL / IS_NOT_NULL / AND / OR -- the operators the API routers
    actually emit when translating query params (LIKE/ILIKE back the
    ``?q=`` search) or declarative checks (CONTAINS backs
    ``ReferenceCheck(op=Op.CONTAINS)``, array-membership reference
    checks like "is this profile a member of any aggregate"). Other
    operators fall through to ``True`` (the test storage is
    intentionally minimal) -- CONTAINS is deliberately NOT one of them:
    an unimplemented array-membership check silently matching every row
    turns a reference-integrity block into a false positive on every
    delete, which is worse than an explicit NotImplementedError would
    have been.
    """
    if isinstance(node, Predicate):
        if node.op in (Op.AND, Op.OR):
            left = _eval_predicate(entity, node.left)
            right = _eval_predicate(entity, node.right)
            return (left and right) if node.op == Op.AND else (left or right)
        # Unary null check: only the left FieldRef matters.
        if node.op in (Op.IS_NULL, Op.IS_NOT_NULL):
            if not isinstance(node.left, FieldRef):
                return True
            actual = _resolve_field(entity, node.left.name)
            if node.op == Op.IS_NULL:
                return actual is None
            return actual is not None
        # Comparison: left is a FieldRef, right is a Value.
        if isinstance(node.left, FieldRef) and isinstance(node.right, Value):
            actual = _resolve_field(entity, node.left.name)
            expected = node.right.value
            if node.op == Op.EQ:
                return actual == expected
            if node.op == Op.NE:
                return actual != expected
            if node.op == Op.GT:
                return actual is not None and actual > expected
            if node.op == Op.LT:
                return actual is not None and actual < expected
            if node.op == Op.GE:
                return actual is not None and actual >= expected
            if node.op == Op.LE:
                return actual is not None and actual <= expected
            if node.op in (Op.LIKE, Op.ILIKE):
                if actual is None or not isinstance(expected, str):
                    return False
                return _like_match(
                    str(actual),
                    expected,
                    case_insensitive=(node.op == Op.ILIKE),
                )
            if node.op == Op.CONTAINS:
                if not isinstance(actual, (list, tuple)):
                    return False
                return expected in actual
        return True
    return True


class _FakeContentStore:
    """In-memory ``DocumentContentStore`` for the fake provider.

    Holds bodies keyed by document id with the same
    ``UNIQUE(collection_id, path)`` semantics as the real backends, so the
    path-addressed :class:`DocumentService` (and the routes that wrap it)
    works over the fake provider too. ``conn`` is accepted and ignored: the
    fake provider's ``transaction()`` is a no-op context manager.
    """

    def __init__(self) -> None:
        # document_id -> ContentRow
        self._rows: dict[str, Any] = {}

    async def ensure_schema(self) -> None:
        return

    async def get(self, document_id: str, *, conn: Any | None = None) -> str | None:
        row = self._rows.get(document_id)
        return row.content if row is not None else None

    async def get_by_path(self, collection_id, path, *, conn=None):
        for row in self._rows.values():
            if row.collection_id == collection_id and row.path == path:
                return row
        return None

    async def resolve_id(self, collection_id, path, *, conn=None):
        row = await self.get_by_path(collection_id, path)
        return row.document_id if row is not None else None

    async def upsert(
        self, *, document_id, collection_id, path, content, conn=None
    ) -> None:
        from primer.int.document_content import ContentRow

        owner = await self.resolve_id(collection_id, path)
        if owner is not None and owner != document_id:
            raise ConflictError(
                f"path {path!r} already taken in collection {collection_id!r}"
            )
        self._rows[document_id] = ContentRow(
            document_id=document_id,
            collection_id=collection_id,
            path=path,
            content=content,
        )

    async def delete(self, document_id, *, conn=None) -> None:
        self._rows.pop(document_id, None)

    async def move(self, document_id, new_path, *, conn=None) -> None:
        row = self._rows.get(document_id)
        if row is None:
            raise NotFoundError(f"no content row for document {document_id!r}")
        owner = await self.resolve_id(row.collection_id, new_path)
        if owner is not None and owner != document_id:
            raise ConflictError(
                f"path {new_path!r} already taken in collection "
                f"{row.collection_id!r}"
            )
        self._rows[document_id] = row.model_copy(update={"path": new_path})

    async def list(self, collection_id, *, prefix=None):
        from primer.int.document_content import ContentListEntry

        entries = [
            ContentListEntry(
                document_id=r.document_id, path=r.path, size=len(r.content)
            )
            for r in self._rows.values()
            if r.collection_id == collection_id
            and (prefix is None or r.path.startswith(prefix))
        ]
        return sorted(entries, key=lambda e: e.path)


class _NoOpTransaction:
    """No-op async context manager for the fake provider's ``transaction()``.

    Yields ``None`` as the connection handle: the fake storage + content
    store ignore ``conn`` and mutate their in-memory dicts directly, so
    there is nothing to commit or roll back.
    """

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc) -> bool:
        return False


#: Profile ids the suite's agent fixtures reference. Shaped
#: ``<provider>--<model>``, matching what migration m002 synthesises.
_TEST_PROFILE_IDS: tuple[str, ...] = (
    "p--m",
    "llm-p--m",
    "x--m",
    "p1--m1",
    "prov--model",
    "prov-1--m1",
    "prov-1--m-1",
    "openai-1--gpt-4o-mini",
    "anthropic-1--claude-sonnet-4-6",
)


async def seed_model_profile(
    storage_provider,
    profile_id: str = "p--m",
    *,
    context_length: int = 128_000,
    config=None,
):
    """Create one ModelProfile row, deriving provider and model from the id.

    For tests running against a real storage backend (where the fake's
    pre-seeding does not apply), or needing a profile with non-default
    config. Idempotent: returns the existing row if one is already there.
    """
    from primer.model.model_profile import ModelProfile, ModelProfileConfig

    store = storage_provider.get_storage(ModelProfile)
    existing = await store.get(profile_id)
    if existing is not None:
        return existing
    provider_id, _, model_name = profile_id.partition("--")
    return await store.create(
        ModelProfile(
            id=profile_id,
            description=f"Test profile {profile_id}.",
            provider_id=provider_id,
            model_name=model_name,
            context_length=context_length,
            config=config or ModelProfileConfig(),
        )
    )


class _FakeStorageProvider:
    """In-memory ``StorageProvider`` returning ``_InMemoryStorage`` per model."""

    def __init__(self) -> None:
        self._stores: dict[type, _InMemoryStorage[Any]] = {}
        self._content_store = _FakeContentStore()
        self._bootstrap_completed_at: datetime | None = None
        self._schema_version: int = 1
        self._last_migration_at: datetime | None = None
        self._seed_model_profiles()

    def _seed_model_profiles(self) -> None:
        """Pre-seed the suite's standard ModelProfile vocabulary.

        Model resolution became storage-backed with the profile cutover:
        an agent names a profile id, and the resolver reads that row. Tests
        across the suite build agents from a small fixed set of ids shaped
        ``<provider>--<model>`` (the same rule migration m002 synthesises
        with), so seeding them once here keeps every agent-building fixture
        runnable instead of making each test restate the same rows.

        Tests that need a profile with non-default config, or that run
        against a real storage backend rather than this fake, should call
        :func:`seed_model_profile` explicitly.
        """
        from primer.model.model_profile import ModelProfile

        store = self.get_storage(ModelProfile)
        for profile_id in _TEST_PROFILE_IDS:
            provider_id, _, model_name = profile_id.partition("--")
            store._data[profile_id] = ModelProfile(
                id=profile_id,
                description=f"Test profile {profile_id}.",
                provider_id=provider_id,
                model_name=model_name,
                context_length=128_000,
            )

    def get_storage(self, model_class: type[_T]) -> _InMemoryStorage[_T]:
        return self._stores.setdefault(model_class, _InMemoryStorage(model_class))

    def get_content_store(self) -> Any:
        return self._content_store

    def get_event_store(self) -> Any:
        from primer.events.memory_store import InMemoryEventStore

        if not hasattr(self, "_event_store"):
            self._event_store = InMemoryEventStore()
        return self._event_store

    def transaction(self) -> Any:
        return _NoOpTransaction()

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def get_system_state(self) -> Any:
        from primer.model.system_state import SystemState
        return SystemState(
            bootstrap_completed_at=self._bootstrap_completed_at,
            schema_version=self._schema_version,
            last_migration_at=self._last_migration_at,
            session_secret=getattr(self, "_session_secret", None),
            sso_jit_enabled=getattr(self, "_sso_jit_enabled", False),
            sso_default_access=getattr(self, "_sso_default_access", None),
            default_agent_id=getattr(self, "_default_agent_id", None),
        )

    async def set_default_agent_id(self, agent_id: str | None) -> None:
        self._default_agent_id = agent_id

    async def set_bootstrap_completed(self, ts: datetime) -> None:
        self._bootstrap_completed_at = ts

    async def set_schema_version(
        self, version: int, *, migrated_at: datetime | None = None,
    ) -> None:
        self._schema_version = version
        self._last_migration_at = migrated_at or datetime.now(UTC)

    async def set_session_secret(self, secret: str) -> None:
        self._session_secret = secret

    async def set_sso_jit_enabled(self, enabled: bool) -> None:
        self._sso_jit_enabled = enabled

    async def set_sso_default_access(self, access: str | None) -> None:
        self._sso_default_access = access


@pytest.fixture
def fake_storage_provider() -> _FakeStorageProvider:
    return _FakeStorageProvider()


@pytest.fixture
def fake_provider_registry(
    fake_storage_provider: _FakeStorageProvider,
) -> Any:
    """Minimal ProviderRegistry shim for tests outside tests/api/.

    The llm_factory and other factories are stubs; tests that need a
    real LLM should monkey-patch ``registry.get_llm`` directly (the
    ``deps`` fixture in tests/chat/test_dispatch.py does this).
    """
    from primer.api.registries import ProviderRegistry

    return ProviderRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )


class _FakeLLM:
    """Minimal fake LLM for worker/chat integration tests.

    Yields a single TextDelta + Done so the chat runner completes
    cleanly without a real Anthropic endpoint.
    """

    def __init__(self, reply_text: str = "ok") -> None:
        self._reply_text = reply_text
        self._stream_factory = None  # optional: callable returning an async iterator
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model: str, messages: Any, **kwargs: Any) -> Any:
        from primer.model.chat import Done, TextDelta

        self.calls.append({"model": model, "messages": list(messages), **kwargs})
        return self._stream_impl()

    async def _stream_impl(self) -> AsyncIterator[Any]:
        from primer.model.chat import Done, TextDelta

        if self._stream_factory is not None:
            async for ev in self._stream_factory():
                yield ev
            return
        yield TextDelta(text=self._reply_text, index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_llm() -> _FakeLLM:
    """Shared fake LLM visible to all test sub-packages."""
    return _FakeLLM()


__all__ = [
    "_FakeStorageProvider",
    "_FakeLLM",
    "_InMemoryStorage",
]
