"""The generic ``create_`` / ``update_`` tools ANSWER a refusal of the secret rules instead of raising it out of the tool (ticket 01a1212a, part B).

``update_`` runs ``admin_when`` (the entity's "only an admin may make this write" predicate) before the write, and a predicate that restores a served mask to decide (``update_toolset``) can refuse it:
the tool answers ``type=validation-error`` with the reason, as the REST route answers a 422. ``create_`` refuses a body that carries a mask a ``get_`` served (the copy-a-row move). An entity with no
secret is unaffected. The real entities are pinned in ``tests/toolset/test_system_masked_secret_origin.py``; this file uses a small entity of its own so the handler's guard is reached directly.
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from primer.model.common import Identifiable
from primer.model.except_ import ValidationError as PrimerValidationError
from primer.toolset._system_crud import _crud_tools_for


class _Widget(Identifiable):
    _id_prefix = "widget"
    token: SecretStr | None = None


def _tools(storage_provider, **options):
    return _crud_tools_for(entity_label="widget", entity_label_plural="widgets", model_cls=_Widget, storage_provider=storage_provider, **options)


def _refuses(entity, existing):
    raise PrimerValidationError("token: re-enter the secret: the stored one is kept only for the same host")


async def _call(tools, name: str, arguments: dict):
    result = await tools[name][1](arguments, None)
    return result, json.loads(result.output)


@pytest.mark.asyncio
async def test_update_answers_a_refusal_raised_by_admin_when(fake_storage_provider) -> None:
    storage = fake_storage_provider.get_storage(_Widget)
    await storage.create(_Widget(id="w-1", token="stored-secret-value"))
    tools = _tools(fake_storage_provider, admin_when=_refuses, admin_note="admin only")

    result, body = await _call(tools, "update_widget", {"id": "w-1", "entity": {"id": "w-1", "token": "another"}})

    assert result.is_error and body["type"] == "validation-error", result.output
    assert "re-enter the secret" in body["message"] and "stored-secret-value" not in result.output
    assert (await storage.get("w-1")).token.get_secret_value() == "stored-secret-value", "nothing was stored"


@pytest.mark.asyncio
async def test_update_still_restores_a_served_mask_for_an_entity_with_no_origin(fake_storage_provider) -> None:
    """The guard the origin rule added must not touch an entity that names no origin: a served mask sent back keeps its stored secret."""
    storage = fake_storage_provider.get_storage(_Widget)
    await storage.create(_Widget(id="w-2", token="stored-secret-value"))
    tools = _tools(fake_storage_provider)

    result, _ = await _call(tools, "update_widget", {"id": "w-2", "entity": {"id": "w-2", "token": "**********alue"}})

    assert not result.is_error, result.output
    assert (await storage.get("w-2")).token.get_secret_value() == "stored-secret-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("served", ["**********", "**********alue"])
async def test_create_refuses_a_body_that_carries_a_served_mask(fake_storage_provider, served: str) -> None:
    storage = fake_storage_provider.get_storage(_Widget)
    tools = _tools(fake_storage_provider)

    result, body = await _call(tools, "create_widget", {"entity": {"id": "w-3", "token": served}})

    assert result.is_error and body["type"] == "validation-error", result.output
    assert "re-enter the secret" in body["message"]
    assert await storage.get("w-3") is None, "nothing was stored"


@pytest.mark.asyncio
async def test_create_accepts_a_real_secret_and_a_body_with_none(fake_storage_provider) -> None:
    storage = fake_storage_provider.get_storage(_Widget)
    tools = _tools(fake_storage_provider)

    real, _ = await _call(tools, "create_widget", {"entity": {"id": "w-4", "token": "a-real-secret-value"}})
    none, _ = await _call(tools, "create_widget", {"entity": {"id": "w-5"}})

    assert not real.is_error and not none.is_error, (real.output, none.output)
    assert (await storage.get("w-4")).token.get_secret_value() == "a-real-secret-value" and (await storage.get("w-5")).token is None
