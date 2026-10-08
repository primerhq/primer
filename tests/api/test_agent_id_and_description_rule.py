"""A NEW agent's id must be a name and its description must not be blank, in the backend (ticket 01a11c1c, ADM-14 follow-up).

#566 made the console refuse, before it posts, an id that is not ``^[a-z0-9][a-z0-9_-]{0,62}$`` and a blank description. That was a UI
convention only: ``POST /v1/agents`` answered 201 for ``Bad Name!`` or for a description of spaces, and so did the system ``create_agent``
tool and the builder's ``crud`` toolset. The rule is now one shared check (``primer/agent/agent_checks.py``) behind all three.

* the id is checked on CREATE only: an existing row keeps whatever id it has (it is immutable, and a deployment may hold ids older than
  the rule), and the seeded and harness-managed agents are written straight to storage, not through the check. An omitted id is generated
  as ``agent-<hex>``, which satisfies the rule;
* a blank description is refused on create, and on an update that MAKES it blank. An update that leaves an already blank description
  alone is accepted, like every other pre-write check (only what the update adds is refused);
* on the route the refusal is a request-validation 422 whose error is at ``body.id`` / ``body.description``, the shape the console's
  ``fieldErrors`` display already reads (the other checks' ``{error, field, message}`` has no entry there); the tools answer
  ``validation-error`` with the field in front.

The id appears in URLs (``/v1/agents/{id}``), in a graph agent node, a session binding and a trigger subscription. It is never part of a
qualified ``<toolset>__<tool>`` name (that is a TOOLSET id), but two underscores in a row are RESERVED for the ids a harness install mints
(``<slug>__<template>``, ``primer/harness/service.py``): a REST-created ``acme__assistant`` would collide with a later install of ``acme``.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest

from primer.bootstrap.defaults import (
    RESERVED_BUILDER_AGENT,
    RESERVED_EXPLORER_AGENT,
    RESERVED_OPERATOR_AGENT,
    RESERVED_PLANNER_AGENT,
    RESERVED_TOOL_RUNNER_AGENT,
)
from primer.model.agent import Agent, AgentModel
from tests._support.agent_check_rows import AGENT_DESCRIPTION_MESSAGE, agent_body, agent_id_message, profile_row
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401

# The rule, spelled out here on purpose: a test that imported it would pass whatever the pattern became.
AGENT_ID_PATTERN = r"^(?!.*__)[a-z0-9][a-z0-9_-]{0,62}$"
BAD_IDS = [
    "Bad Name!", "RefundTriage", "refund triage", "-leading", "_leading", "refund/triage", "refund.triage", "refund%20triage",
    "naïve", " refund", "refund ", "refund\n", "a" * 64, "..", "a/../b",
    # two underscores in a row are reserved for the ids a harness install mints (<slug>__<template>)
    "a__b", "acme__assistant", "x___y", "ends__", "a_-__b",
]
GOOD_IDS = ["refund-triage", "my_agent_1", "a", "0day", "agent-3f9a1c8d", "a_b_c", "a--b", "a-_-b", "a" * 63]
BLANK_DESCRIPTIONS = ["", " ", "   ", "\n\t "]


async def _seed(app, row) -> None:
    await app.state.storage_provider.get_storage(type(row)).create(row)


def _errors(response) -> list[dict]:
    return response.json()["extensions"]["errors"]


# ---- the id -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", BAD_IDS)
async def test_a_new_agent_whose_id_is_not_a_name_is_refused_at_body_id_and_not_stored(client, app, agent_id: str) -> None:
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json=agent_body(agent_id))

    assert r.status_code == 422, r.text
    assert [(e["loc"], e["type"], e["msg"]) for e in _errors(r)] == [(["body", "id"], "agent_id_invalid", agent_id_message(agent_id))]
    assert await app.state.storage_provider.get_storage(Agent).get(agent_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", GOOD_IDS)
async def test_a_new_agent_whose_id_is_a_name_is_created_and_reachable_by_its_url(client, app, agent_id: str) -> None:
    await _seed(app, profile_row("mp-1"))

    created = await client.post("/v1/agents", json=agent_body(agent_id))
    fetched = await client.get(f"/v1/agents/{agent_id}")

    assert created.status_code == 201, created.text
    assert fetched.status_code == 200 and fetched.json()["id"] == agent_id
    assert quote(agent_id, safe="") == agent_id, "a name needs no URL escaping"


@pytest.mark.asyncio
async def test_an_agent_created_without_an_id_gets_a_generated_one_that_satisfies_the_rule(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    body = agent_body("placeholder")
    del body["id"]

    r = await client.post("/v1/agents", json=body)

    assert r.status_code == 201, r.text
    assert re.fullmatch(AGENT_ID_PATTERN, r.json()["id"]) and r.json()["id"].startswith("agent-")


def test_the_backend_constant_is_the_rule_the_console_form_and_these_tests_spell_out() -> None:
    from primer.agent.agent_checks import AGENT_ID_PATTERN as backend

    assert backend == AGENT_ID_PATTERN


def test_the_seeded_agents_and_the_generated_prefix_satisfy_the_rule() -> None:
    """They are written straight to storage, so the rule never sees them; still, nothing the platform makes may break it."""
    for seeded in (
        RESERVED_OPERATOR_AGENT, RESERVED_BUILDER_AGENT, RESERVED_PLANNER_AGENT, RESERVED_EXPLORER_AGENT, RESERVED_TOOL_RUNNER_AGENT,
    ):
        assert re.fullmatch(AGENT_ID_PATTERN, seeded), seeded
    generated = Agent(description="d", model=AgentModel(profile_id="mp-1")).id
    assert generated and re.fullmatch(AGENT_ID_PATTERN, generated), generated


@pytest.mark.asyncio
async def test_an_existing_agent_with_an_id_older_than_the_rule_still_reads_lists_and_updates(client, app) -> None:
    """The rule is for NEW agents: a stored ``Legacy_Name`` is neither hidden nor made un-editable."""
    await _seed(app, profile_row("mp-1"))
    await _seed(app, Agent.model_validate(agent_body("Legacy_Name")))

    got = await client.get("/v1/agents/Legacy_Name")
    listed = await client.get("/v1/agents")
    edited = await client.put("/v1/agents/Legacy_Name", json={**agent_body("Legacy_Name"), "description": "now described"})

    assert got.status_code == 200, got.text
    assert "Legacy_Name" in [a["id"] for a in listed.json()["items"]]
    assert edited.status_code == 200 and edited.json()["description"] == "now described", edited.text


# ---- the description ----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("description", BLANK_DESCRIPTIONS)
async def test_a_new_agent_with_a_blank_description_is_refused_at_body_description_and_not_stored(client, app, description: str) -> None:
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json={**agent_body("ag-1"), "description": description})

    assert r.status_code == 422, r.text
    assert [(e["loc"], e["type"], e["msg"]) for e in _errors(r)] == [
        (["body", "description"], "agent_description_blank", AGENT_DESCRIPTION_MESSAGE)
    ]
    assert await app.state.storage_provider.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
async def test_a_description_with_words_and_surrounding_space_is_accepted_as_it_is(client, app) -> None:
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json={**agent_body("ag-1"), "description": "  triages refunds  "})

    assert r.status_code == 201, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("description", BLANK_DESCRIPTIONS)
async def test_an_update_that_makes_the_description_blank_is_refused_and_leaves_the_row(client, app, description: str) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed(app, Agent.model_validate(agent_body("ag-1")))

    r = await client.put("/v1/agents/ag-1", json={**agent_body("ag-1"), "description": description})

    assert r.status_code == 422, r.text
    assert [(e["loc"], e["type"]) for e in _errors(r)] == [(["body", "description"], "agent_description_blank")]
    assert (await app.state.storage_provider.get_storage(Agent).get("ag-1")).description == "a test agent"


@pytest.mark.asyncio
async def test_an_agent_that_is_already_blank_can_still_be_edited_without_describing_it(client, app) -> None:
    """Like every other pre-write check, an update refuses only what it ADDS: this row was stored before the rule."""
    await _seed(app, profile_row("mp-1"))
    await _seed(app, Agent.model_validate({**agent_body("ag-1"), "description": ""}))

    r = await client.put("/v1/agents/ag-1", json={**agent_body("ag-1"), "description": "", "temperature": 0.5})

    assert r.status_code == 200, r.text
    assert r.json()["temperature"] == 0.5


@pytest.mark.asyncio
async def test_an_update_may_set_a_description_on_a_blank_agent(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed(app, Agent.model_validate({**agent_body("ag-1"), "description": ""}))

    r = await client.put("/v1/agents/ag-1", json={**agent_body("ag-1"), "description": "triages refunds"})

    assert r.status_code == 200, r.text


# ---- both, and the order ------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_id_is_reported_before_the_description_and_both_before_the_references(client, app) -> None:
    both = await client.post("/v1/agents", json={**agent_body("Bad Name!", profile_id="ghost-profile"), "description": " "})

    assert both.status_code == 422
    assert [e["loc"] for e in _errors(both)] == [["body", "id"]], "one refusal at a time, the id first"
    assert "model_profile_not_found" not in both.text


# ---- the whole body -----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_whole_refusal_body_is_the_request_validation_envelope(client, app) -> None:
    """Pinned like the reference checks' bodies: status, content type, the envelope and the key order (the ``request_id`` normalised)."""
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json=agent_body("Bad Name!"))

    assert r.status_code == 422 and r.headers["content-type"] == "application/problem+json"
    assert re.sub(r"req-[0-9a-f]+", "req-<id>", r.text) == (
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,'
        '"detail":"One or more request parameters or body fields failed validation.","instance":"/v1/agents",'
        '"extensions":{"errors":[{"type":"agent_id_invalid","loc":["body","id"],"msg":'
        + '"' + agent_id_message("Bad Name!").replace('"', '\\"') + '"'
        + '}],"request_id":"req-<id>"}}'
    )


@pytest.mark.asyncio
async def test_an_agent_stored_with_an_older_id_can_be_read_updated_and_deleted(client, app) -> None:
    """The rule is for NEW agents. A stored ``Legacy_Name``, and one with a double underscore that predates the reservation, are neither hidden nor stuck."""
    await _seed(app, profile_row("mp-1"))
    await _seed(app, Agent.model_validate(agent_body("Legacy_Name")))
    await _seed(app, Agent.model_validate(agent_body("old__style")))

    got = await client.get("/v1/agents/old__style")
    edited = await client.put("/v1/agents/old__style", json={**agent_body("old__style"), "description": "now described"})
    deleted_a = await client.delete("/v1/agents/Legacy_Name")
    deleted_b = await client.delete("/v1/agents/old__style")

    assert got.status_code == 200 and edited.status_code == 200, (got.text, edited.text)
    assert deleted_a.status_code in (200, 204) and deleted_b.status_code in (200, 204), (deleted_a.text, deleted_b.text)
    storage = app.state.storage_provider.get_storage(Agent)
    assert await storage.get("Legacy_Name") is None and await storage.get("old__style") is None


def test_the_reservation_is_the_separator_the_harness_install_uses() -> None:
    """Harness installs mint ``<slug>__<template>`` (primer/harness/service.py resolved_id). If that separator ever changes, the reservation must follow."""
    from primer.harness.service import resolved_id

    minted = resolved_id("acme", "assistant")

    assert "__" in minted and re.fullmatch(AGENT_ID_PATTERN, minted) is None
