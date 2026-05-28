"""Unit tests for the ``harness`` internal toolset (Task 12).

Covers:
1. harness__register creates a DRAFT row.
2. harness__register rejects duplicate slug.
3. harness__list returns existing rows; filters by slug + status.
4. harness__get returns one row; 404 when missing.
5. harness__fetch flips pending_operation; rejected with 409 if already pending.
6. harness__update_overrides validates against schema.
7. harness__uninstall enqueues uninstall.
8. Tool tokens are stored encrypted (SecretStr).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.model.harness import Harness, HarnessOperation, HarnessStatus
from primer.model.storage import FieldRef, OffsetPage, OffsetPageResponse, Op, Predicate, Value
from primer.toolset.harness import HARNESS_TOOLSET_ID, build_harness_toolset_provider
from pydantic import SecretStr


# ===========================================================================
# In-memory fakes
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str):
        return self._data.get(id)

    async def create(self, entity):
        from primer.model.except_ import ConflictError

        if entity.id in self._data:
            raise ConflictError(f"id {entity.id!r} already exists")
        self._data[entity.id] = entity
        return entity

    async def update(self, entity):
        from primer.model.except_ import NotFoundError

        if entity.id not in self._data:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str):
        from primer.model.except_ import NotFoundError

        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            sliced = items[page.offset: page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        return OffsetPageResponse(offset=0, length=len(items), total=len(items), items=items)

    async def find(self, predicate, page, *, order_by=None):
        """Evaluate predicate against all rows and return matching items."""
        items = [
            item
            for item in self._data.values()
            if _eval_predicate(predicate, item)
        ]
        if isinstance(page, OffsetPage):
            sliced = items[page.offset: page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        return OffsetPageResponse(offset=0, length=len(items), total=len(items), items=items)


def _eval_predicate(pred: Predicate, obj: Any) -> bool:
    """Minimal recursive predicate evaluator for tests."""
    left, op, right = pred.left, pred.op, pred.right

    if op == Op.AND:
        return _eval_predicate(left, obj) and _eval_predicate(right, obj)
    if op == Op.OR:
        return _eval_predicate(left, obj) or _eval_predicate(right, obj)

    # leaf: left is FieldRef, right is Value
    if isinstance(left, FieldRef) and isinstance(right, Value):
        field_val = getattr(obj, left.name, None)
        # For enums, compare .value
        if hasattr(field_val, "value"):
            field_val = field_val.value
        if op == Op.EQ:
            return field_val == right.value
        if op == Op.NE:
            return field_val != right.value
    return False


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls):
        return self._stores.setdefault(cls, _Storage())


class _EventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, key: str, payload: Any = None) -> None:
        self.published.append((key, payload))


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp():
    return _SP()


@pytest.fixture
def event_bus():
    return _EventBus()


@pytest.fixture
def toolset(sp, event_bus):
    return build_harness_toolset_provider(
        storage_provider=sp,
        event_bus=event_bus,
    )


def _result(data: dict) -> dict:
    """Parse ToolCallResult output as dict."""
    return json.loads(data.output)


def _make_harness(sp: _SP, **kwargs) -> Harness:
    """Insert a harness directly into the fake storage."""
    defaults = dict(
        id="hns_aabbcc001",
        slug="test-harness",
        name="Test Harness",
        git_url="https://github.com/example/repo",
        status=HarnessStatus.DRAFT,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    harness = Harness(**defaults)
    sp.get_storage(Harness)._data[harness.id] = harness
    return harness


# ===========================================================================
# 1. harness__register creates a DRAFT row
# ===========================================================================


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_creates_draft_row(self, toolset) -> None:
        result = await toolset.call(
            tool_name="harness__register",
            arguments={
                "name": "My Harness",
                "slug": "my-harness",
                "git_url": "https://github.com/example/repo",
            },
        )
        assert not result.is_error, result.output
        body = _result(result)
        assert body["status"] == "draft"
        assert body["slug"] == "my-harness"
        assert body["name"] == "My Harness"
        assert body["id"].startswith("hns_")
        assert body["ref"] == "main"

    @pytest.mark.asyncio
    async def test_register_stores_token_as_secret_str(self, toolset, sp) -> None:
        result = await toolset.call(
            tool_name="harness__register",
            arguments={
                "name": "Tokenized",
                "slug": "tokenized-harness",
                "git_url": "https://github.com/example/repo",
                "git_token": "secret-token-value",
            },
        )
        assert not result.is_error
        body = _result(result)
        harness_id = body["id"]
        # The token in the response should be redacted
        assert body["git_token"] == "**********"

        # The stored entity should have a SecretStr (not a plain string)
        stored: Harness = sp.get_storage(Harness)._data[harness_id]
        assert isinstance(stored.git_token, SecretStr)
        assert stored.git_token.get_secret_value() == "secret-token-value"

    @pytest.mark.asyncio
    async def test_register_uses_custom_ref(self, toolset) -> None:
        result = await toolset.call(
            tool_name="harness__register",
            arguments={
                "name": "Dev branch",
                "slug": "dev-harness",
                "git_url": "https://github.com/example/repo",
                "ref": "develop",
            },
        )
        assert not result.is_error
        body = _result(result)
        assert body["ref"] == "develop"

    # 2. harness__register rejects duplicate slug

    @pytest.mark.asyncio
    async def test_register_rejects_duplicate_slug(self, toolset) -> None:
        args = {
            "name": "First",
            "slug": "dup-slug",
            "git_url": "https://github.com/example/repo",
        }
        r1 = await toolset.call(tool_name="harness__register", arguments=args)
        assert not r1.is_error

        r2 = await toolset.call(
            tool_name="harness__register",
            arguments={"name": "Second", "slug": "dup-slug", "git_url": "https://github.com/example/repo2"},
        )
        assert r2.is_error
        body = _result(r2)
        assert body["type"] == "conflict"


# ===========================================================================
# 3. harness__list
# ===========================================================================


class TestList:
    @pytest.mark.asyncio
    async def test_list_returns_all(self, toolset, sp) -> None:
        _make_harness(sp, id="hns_a1", slug="harness-a", name="A")
        _make_harness(sp, id="hns_b1", slug="harness-b", name="B")

        result = await toolset.call(tool_name="harness__list", arguments={})
        assert not result.is_error
        body = _result(result)
        assert "items" in body
        assert body["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_filter_by_slug(self, toolset, sp) -> None:
        _make_harness(sp, id="hns_a2", slug="filter-slug", name="Target")
        _make_harness(sp, id="hns_b2", slug="other-slug", name="Other")

        result = await toolset.call(
            tool_name="harness__list",
            arguments={"slug": "filter-slug"},
        )
        assert not result.is_error
        body = _result(result)
        assert body["total"] == 1
        assert body["items"][0]["slug"] == "filter-slug"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, toolset, sp) -> None:
        _make_harness(sp, id="hns_a3", slug="ready-one", name="Ready", status=HarnessStatus.READY)
        _make_harness(sp, id="hns_b3", slug="draft-one", name="Draft", status=HarnessStatus.DRAFT)

        result = await toolset.call(
            tool_name="harness__list",
            arguments={"status": "ready"},
        )
        assert not result.is_error
        body = _result(result)
        assert body["total"] == 1
        assert body["items"][0]["status"] == "ready"


# ===========================================================================
# 4. harness__get
# ===========================================================================


class TestGet:
    @pytest.mark.asyncio
    async def test_get_returns_row(self, toolset, sp) -> None:
        h = _make_harness(sp)
        result = await toolset.call(
            tool_name="harness__get",
            arguments={"id": h.id},
        )
        assert not result.is_error
        body = _result(result)
        assert body["id"] == h.id
        assert body["slug"] == h.slug

    @pytest.mark.asyncio
    async def test_get_404_when_missing(self, toolset) -> None:
        result = await toolset.call(
            tool_name="harness__get",
            arguments={"id": "hns_nonexistent"},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "not-found"


# ===========================================================================
# 5. harness__fetch — flips pending_operation; 409 if already pending
# ===========================================================================


class TestFetch:
    @pytest.mark.asyncio
    async def test_fetch_enqueues_operation(self, toolset, sp, event_bus) -> None:
        h = _make_harness(sp)
        result = await toolset.call(
            tool_name="harness__fetch",
            arguments={"id": h.id},
        )
        assert not result.is_error
        body = _result(result)
        assert body["pending_operation"] == "fetch"
        # Event published
        assert any(key == "harness-claimable" for key, _ in event_bus.published)

    @pytest.mark.asyncio
    async def test_fetch_409_when_already_pending(self, toolset, sp) -> None:
        h = _make_harness(sp)
        # Set an existing pending operation
        h.pending_operation = HarnessOperation.INSTALL
        sp.get_storage(Harness)._data[h.id] = h

        result = await toolset.call(
            tool_name="harness__fetch",
            arguments={"id": h.id},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_fetch_404_when_missing(self, toolset) -> None:
        result = await toolset.call(
            tool_name="harness__fetch",
            arguments={"id": "hns_ghost"},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "not-found"


# ===========================================================================
# 6. harness__update_overrides validates against schema
# ===========================================================================


class TestUpdateOverrides:
    @pytest.mark.asyncio
    async def test_update_overrides_valid(self, toolset, sp) -> None:
        h = _make_harness(sp)
        h.overrides_schema = {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        }
        sp.get_storage(Harness)._data[h.id] = h

        result = await toolset.call(
            tool_name="harness__update_overrides",
            arguments={"id": h.id, "overrides": {"region": "us-east-1"}},
        )
        assert not result.is_error
        body = _result(result)
        assert body["overrides"] == {"region": "us-east-1"}

    @pytest.mark.asyncio
    async def test_update_overrides_invalid_against_schema(self, toolset, sp) -> None:
        h = _make_harness(sp)
        h.overrides_schema = {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "required": ["region"],
        }
        sp.get_storage(Harness)._data[h.id] = h

        result = await toolset.call(
            tool_name="harness__update_overrides",
            arguments={"id": h.id, "overrides": {"region": 999}},  # invalid type
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "overrides-invalid"

    @pytest.mark.asyncio
    async def test_update_overrides_no_schema_cached(self, toolset, sp) -> None:
        h = _make_harness(sp)  # no overrides_schema
        result = await toolset.call(
            tool_name="harness__update_overrides",
            arguments={"id": h.id, "overrides": {"x": "y"}},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "overrides-schema-missing"


# ===========================================================================
# 7. harness__uninstall enqueues uninstall
# ===========================================================================


class TestUninstall:
    @pytest.mark.asyncio
    async def test_uninstall_enqueues_operation(self, toolset, sp, event_bus) -> None:
        h = _make_harness(sp)
        result = await toolset.call(
            tool_name="harness__uninstall",
            arguments={"id": h.id},
        )
        assert not result.is_error
        body = _result(result)
        assert body["pending_operation"] == "uninstall"
        assert any(key == "harness-claimable" for key, _ in event_bus.published)

    @pytest.mark.asyncio
    async def test_uninstall_409_when_already_pending(self, toolset, sp) -> None:
        h = _make_harness(sp)
        h.pending_operation = HarnessOperation.FETCH
        sp.get_storage(Harness)._data[h.id] = h

        result = await toolset.call(
            tool_name="harness__uninstall",
            arguments={"id": h.id},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "conflict"


# ===========================================================================
# Catalog tests
# ===========================================================================


class TestCatalog:
    @pytest.mark.asyncio
    async def test_toolset_id_and_tool_count(self, toolset) -> None:
        names = [t.id async for t in toolset.list_tools()]
        expected = {
            "harness__list",
            "harness__get",
            "harness__register",
            "harness__update",
            "harness__update_overrides",
            "harness__fetch",
            "harness__install",
            "harness__sync",
            "harness__uninstall",
        }
        assert set(names) == expected

    @pytest.mark.asyncio
    async def test_all_tools_have_correct_toolset_id(self, toolset) -> None:
        async for tool in toolset.list_tools():
            assert tool.toolset_id == HARNESS_TOOLSET_ID


# ===========================================================================
# harness__update
# ===========================================================================


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_name(self, toolset, sp) -> None:
        h = _make_harness(sp)
        result = await toolset.call(
            tool_name="harness__update",
            arguments={"id": h.id, "name": "New Name"},
        )
        assert not result.is_error
        body = _result(result)
        assert body["name"] == "New Name"

    @pytest.mark.asyncio
    async def test_update_git_token_stores_secret(self, toolset, sp) -> None:
        h = _make_harness(sp)
        result = await toolset.call(
            tool_name="harness__update",
            arguments={"id": h.id, "git_token": "new-secret"},
        )
        assert not result.is_error
        body = _result(result)
        # Response redacts the token
        assert body["git_token"] == "**********"

        stored: Harness = sp.get_storage(Harness)._data[h.id]
        assert isinstance(stored.git_token, SecretStr)
        assert stored.git_token.get_secret_value() == "new-secret"

    @pytest.mark.asyncio
    async def test_update_404_when_missing(self, toolset) -> None:
        result = await toolset.call(
            tool_name="harness__update",
            arguments={"id": "hns_ghost", "name": "Ghost"},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "not-found"


# ===========================================================================
# harness__install + harness__sync (enqueue tools)
# ===========================================================================


class TestInstall:
    @pytest.mark.asyncio
    async def test_install_requires_schema(self, toolset, sp) -> None:
        h = _make_harness(sp, status=HarnessStatus.READY)
        # No overrides_schema cached
        result = await toolset.call(
            tool_name="harness__install",
            arguments={"id": h.id},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "overrides-schema-missing"

    @pytest.mark.asyncio
    async def test_install_enqueues_when_schema_present(self, toolset, sp, event_bus) -> None:
        h = _make_harness(sp, status=HarnessStatus.READY)
        h.overrides_schema = {"type": "object", "properties": {}}
        sp.get_storage(Harness)._data[h.id] = h

        result = await toolset.call(
            tool_name="harness__install",
            arguments={"id": h.id},
        )
        assert not result.is_error
        body = _result(result)
        assert body["pending_operation"] == "install"
        assert any(key == "harness-claimable" for key, _ in event_bus.published)

    @pytest.mark.asyncio
    async def test_install_409_when_already_pending(self, toolset, sp) -> None:
        h = _make_harness(sp, status=HarnessStatus.READY)
        h.pending_operation = HarnessOperation.FETCH
        h.overrides_schema = {"type": "object"}
        sp.get_storage(Harness)._data[h.id] = h

        result = await toolset.call(
            tool_name="harness__install",
            arguments={"id": h.id},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "conflict"


class TestSync:
    @pytest.mark.asyncio
    async def test_sync_requires_installed_status(self, toolset, sp) -> None:
        h = _make_harness(sp, status=HarnessStatus.DRAFT)
        result = await toolset.call(
            tool_name="harness__sync",
            arguments={"id": h.id},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_sync_requires_bundle(self, toolset, sp) -> None:
        h = _make_harness(sp, status=HarnessStatus.INSTALLED)
        # No available_bundle_hash
        result = await toolset.call(
            tool_name="harness__sync",
            arguments={"id": h.id},
        )
        assert result.is_error
        body = _result(result)
        assert body["type"] == "fetch-required"

    @pytest.mark.asyncio
    async def test_sync_enqueues_when_ready(self, toolset, sp, event_bus) -> None:
        h = _make_harness(sp, status=HarnessStatus.INSTALLED)
        h.available_bundle_hash = "abc123"
        sp.get_storage(Harness)._data[h.id] = h

        result = await toolset.call(
            tool_name="harness__sync",
            arguments={"id": h.id},
        )
        assert not result.is_error
        body = _result(result)
        assert body["pending_operation"] == "sync"
        assert any(key == "harness-claimable" for key, _ in event_bus.published)
