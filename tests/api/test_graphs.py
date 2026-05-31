"""CRUD coverage for the /v1/graphs router with Begin/End topology.

Spec §7.1 acceptance: the REST surface accepts/rejects the new node
shapes (Begin / End), preserves conditional edges with
:class:`BranchCondition` lists across save+load round-trips, and emits
422 on topology violations (two Begin nodes, missing End, unreachable
End, malformed JSON Schema).

Uses the shared ``client`` fixture from ``tests/api/conftest.py``:
in-memory storage + auto-registered test user. Each test posts a fresh
graph body and asserts the public-API contract.
"""

from __future__ import annotations

import pytest


# ===========================================================================
# Helpers — minimal graph bodies expressed as plain dicts (the request body
# the REST surface receives is JSON, not Pydantic instances).
# ===========================================================================


def _minimal_begin_end_body(
    graph_id: str = "g-min",
    *,
    description: str = "minimal Begin -> End",
) -> dict:
    """Smallest topologically-valid graph: Begin -> End via a static edge."""
    return {
        "id": graph_id,
        "description": description,
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "end", "id": "finish"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "finish"},
        ],
    }


def _begin_decider_end_body_with_branch(
    graph_id: str = "g-branch",
) -> dict:
    """Begin -> Decider (agent) -> ConditionalEdge -> End with one branch."""
    return {
        "id": graph_id,
        "description": "Begin -> Decider -> End",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {
                "kind": "agent",
                "id": "decider",
                "agent_id": "ag-decider",
                "response_format": {
                    "type": "object",
                    "properties": {"go": {"type": "string"}},
                },
            },
            {"kind": "end", "id": "finish"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "decider"},
            {
                "kind": "conditional",
                "from_node": "decider",
                "router": {
                    "kind": "json_path",
                    "branches": [
                        {
                            "conditions": [
                                {"path": "go", "op": "eq", "value": "finish"},
                            ],
                            "to_node": "finish",
                        },
                    ],
                    "default_to": "finish",
                },
            },
        ],
    }


# ===========================================================================
# Create — happy path + topology violations
# ===========================================================================


@pytest.mark.asyncio
async def test_create_minimal_begin_end_graph_returns_201(client) -> None:
    """A graph with one Begin, one End and a static edge wired between
    them is the smallest valid topology. The router accepts it as-is."""
    body = _minimal_begin_end_body()
    resp = await client.post("/v1/graphs", json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["id"] == "g-min"
    # Round-trip preserves node kinds + ids.
    kinds = sorted(n["kind"] for n in out["nodes"])
    assert kinds == ["begin", "end"]


@pytest.mark.asyncio
async def test_create_two_begin_nodes_returns_422(client) -> None:
    """Topology rule (spec §1.5): exactly one Begin node per graph."""
    body = _minimal_begin_end_body("g-two-begin")
    # Inject a second Begin node — both with valid ids.
    body["nodes"].insert(
        0,
        {"kind": "begin", "id": "rogue_begin"},
    )
    resp = await client.post("/v1/graphs", json=body)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_no_end_node_returns_422(client) -> None:
    """Topology rule (spec §1.5): at least one End node per graph."""
    body = {
        "id": "g-no-end",
        "description": "missing End",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "a", "agent_id": "ag-x"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "a"},
        ],
    }
    resp = await client.post("/v1/graphs", json=body)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_unreachable_end_returns_422(client) -> None:
    """Topology rule (spec §1.5): every End reachable from Begin via forward BFS."""
    body = {
        "id": "g-unreachable",
        "description": "End not reachable from Begin",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "end", "id": "reachable"},
            {"kind": "end", "id": "orphan"},  # no incoming edges
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "reachable"},
        ],
    }
    resp = await client.post("/v1/graphs", json=body)
    assert resp.status_code == 422, resp.text
    # The error message should reference the orphan node id.
    assert "orphan" in resp.text


# ===========================================================================
# Round-trip — conditional edges with BranchCondition lists preserved
# ===========================================================================


@pytest.mark.asyncio
async def test_get_roundtrips_begin_end_with_conditional_edge(client) -> None:
    """A POST + GET on the same graph returns an equivalent shape — the
    conditional edge's BranchCondition list (path + op + value) survives
    the serialise / deserialise round-trip."""
    body = _begin_decider_end_body_with_branch("g-rt")
    post = await client.post("/v1/graphs", json=body)
    assert post.status_code == 201, post.text

    get = await client.get("/v1/graphs/g-rt")
    assert get.status_code == 200, get.text
    out = get.json()

    # Find the conditional edge.
    cond_edges = [e for e in out["edges"] if e["kind"] == "conditional"]
    assert len(cond_edges) == 1
    router = cond_edges[0]["router"]
    assert router["kind"] == "json_path"
    assert router["default_to"] == "finish"
    branches = router["branches"]
    assert len(branches) == 1
    conds = branches[0]["conditions"]
    assert conds == [{"path": "go", "op": "eq", "value": "finish"}]
    assert branches[0]["to_node"] == "finish"


@pytest.mark.asyncio
async def test_put_updates_branch_conditions(client) -> None:
    """A PUT with a modified branch (different op + value) overwrites the
    stored row; a follow-up GET sees the new shape."""
    body = _begin_decider_end_body_with_branch("g-put")
    post = await client.post("/v1/graphs", json=body)
    assert post.status_code == 201, post.text

    # Mutate the one branch's condition: change op eq->ne and value.
    cond_edge = next(e for e in body["edges"] if e["kind"] == "conditional")
    cond_edge["router"]["branches"][0]["conditions"] = [
        {"path": "go", "op": "ne", "value": "abort"},
    ]
    put = await client.put("/v1/graphs/g-put", json=body)
    assert put.status_code == 200, put.text

    get = await client.get("/v1/graphs/g-put")
    assert get.status_code == 200
    out = get.json()
    cond = next(e for e in out["edges"] if e["kind"] == "conditional")
    conds = cond["router"]["branches"][0]["conditions"]
    assert conds == [{"path": "go", "op": "ne", "value": "abort"}]


# ===========================================================================
# JSON Schema validation at graph save time (Task 10.2 surface area)
# ===========================================================================


@pytest.mark.asyncio
async def test_create_with_invalid_input_schema_returns_422(client) -> None:
    """A malformed JSON Schema on Begin.input_schema is rejected at the
    REST layer with 422 — the model-level validator (Task 10.2) raises
    ValidationError, which FastAPI translates to 422."""
    body = _minimal_begin_end_body("g-bad-schema")
    # `type` must be a string or list of strings — passing an int is a
    # JSON Schema meta-schema violation.
    body["nodes"][0]["input_schema"] = {"type": 123}
    resp = await client.post("/v1/graphs", json=body)
    assert resp.status_code == 422, resp.text
