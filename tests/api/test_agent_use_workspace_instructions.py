"""``Agent.use_workspace_instructions`` over REST, on a REAL SQLite store (pivot P2, Task 16).

An agent opts out of its workspace's agents.md with this flag (``primer/workspace/instructions.py``). The route is the generic agent CRUD
router, so the field rides on the model; what these tests pin is that it is stored, served and kept, and that an agent row written before
the field existed reads as ON. No migration writes the key: a row without it decodes to the model default, which is what the stored-row
test below proves on the real backend (the in-memory store of the other REST tests keeps model instances, so it could not see a row
without the key).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.model.agent import Agent
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.agent_check_rows import agent_body, profile_row


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "agents.sqlite")))
    await provider.initialize()
    await provider.get_storage(type(profile_row("mp-1"))).create(profile_row("mp-1"))
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest_asyncio.fixture
async def client(sp: SqliteStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:  # noqa: BLE001 - auth may be off in the test app
            pass
        yield c


async def strip_the_key(sp: SqliteStorageProvider, agent_id: str) -> None:
    """Make the stored row look like one written before the field existed: remove the key from the stored JSON, bypassing the model."""
    table = sp.get_storage(Agent)._table  # noqa: SLF001
    await sp.connection.execute(f"UPDATE \"{table}\" SET data = json_remove(data, '$.use_workspace_instructions') WHERE id = ?", (agent_id,))
    await sp.connection.commit()
    cursor = await sp.connection.execute(f"SELECT data FROM \"{table}\" WHERE id = ?", (agent_id,))
    (data,) = await cursor.fetchone()
    assert "use_workspace_instructions" not in data, "the row now has no key for the field"


@pytest.mark.asyncio
async def test_a_new_agent_is_on_by_default(client, sp) -> None:
    body = agent_body("plain")
    body.pop("use_workspace_instructions", None)

    created = await client.post("/v1/agents", json=body)

    assert created.status_code == 201, created.text
    assert created.json()["use_workspace_instructions"] is True
    assert (await sp.get_storage(Agent).get("plain")).use_workspace_instructions is True


@pytest.mark.asyncio
async def test_an_agent_created_off_is_stored_and_served_off(client, sp) -> None:
    created = await client.post("/v1/agents", json={**agent_body("reviewer"), "use_workspace_instructions": False})

    assert created.status_code == 201, created.text
    assert (await client.get("/v1/agents/reviewer")).json()["use_workspace_instructions"] is False
    assert (await sp.get_storage(Agent).get("reviewer")).use_workspace_instructions is False


@pytest.mark.asyncio
async def test_a_put_of_the_served_body_keeps_it_off_and_a_put_can_turn_it_back_on(client, sp) -> None:
    await client.post("/v1/agents", json={**agent_body("reviewer"), "use_workspace_instructions": False})
    served = (await client.get("/v1/agents/reviewer")).json()

    kept = await client.put("/v1/agents/reviewer", json={**served, "description": "edited"})
    assert kept.status_code == 200, kept.text
    assert (await sp.get_storage(Agent).get("reviewer")).use_workspace_instructions is False

    on = await client.put("/v1/agents/reviewer", json={**served, "use_workspace_instructions": True})
    assert on.status_code == 200, on.text
    assert (await sp.get_storage(Agent).get("reviewer")).use_workspace_instructions is True


@pytest.mark.asyncio
async def test_a_put_that_leaves_the_field_out_turns_it_back_on(client, sp) -> None:
    """PUT replaces the whole row, as for every other field with a default: a caller that writes an agent sends the field it read."""
    await client.post("/v1/agents", json={**agent_body("reviewer"), "use_workspace_instructions": False})
    served = (await client.get("/v1/agents/reviewer")).json()
    served.pop("use_workspace_instructions")

    r = await client.put("/v1/agents/reviewer", json=served)

    assert r.status_code == 200, r.text
    assert r.json()["use_workspace_instructions"] is True


@pytest.mark.asyncio
async def test_a_value_that_is_not_a_boolean_is_refused_at_its_field(client, sp) -> None:
    r = await client.post("/v1/agents", json={**agent_body("reviewer"), "use_workspace_instructions": "maybe"})

    assert r.status_code == 422, r.text
    assert [e["loc"] for e in r.json()["extensions"]["errors"]] == [["body", "use_workspace_instructions"]]
    assert await sp.get_storage(Agent).get("reviewer") is None


@pytest.mark.asyncio
async def test_a_row_stored_before_the_field_existed_reads_on_everywhere(client, sp) -> None:
    await client.post("/v1/agents", json={**agent_body("old-agent"), "use_workspace_instructions": False})
    await strip_the_key(sp, "old-agent")

    assert (await sp.get_storage(Agent).get("old-agent")).use_workspace_instructions is True
    assert (await client.get("/v1/agents/old-agent")).json()["use_workspace_instructions"] is True
    listed = {row["id"]: row for row in (await client.get("/v1/agents")).json()["items"]}
    assert listed["old-agent"]["use_workspace_instructions"] is True
