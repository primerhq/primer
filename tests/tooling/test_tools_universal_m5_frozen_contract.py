"""M5 frozen contract: the reviewed Tools Universal universe, digested.

Restamped three times since the publisher: the curated system__inspect_service_bundle descriptor (one tool id),
the parked-session priority route (one generated id and one operation), and the model settings
catalogue route (one generated id and one operation). From the live
production projection and the reviewed manifest:

* 409 operation identities (operationId, method, path)
* 494 scoped tool ids (exact set, from the reviewed tool catalogue)
* 266 generated declared ids
* the 135 curated pairs (operation -> canonically sorted ids)
* the 8 skips (method, path, skip_code), cross-checked against the manifest's
  reviewed skip digest
* the aggregation set == empty
* the manifest baseline commit

Plus the reference-path freeze: every tool has exactly one
``reference/tools/<toolset>/<tool>`` path, no duplicates.
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.api.route_tools import coverage_projection
from primer.knowledge.wiki.reference import reference_path
from scripts import tools_universal_inventory as inventory

MANIFEST_PATH = Path("scripts/data/tools_universal_m0_manifest.json")

BASE_COMMIT = "fb18c24b92bec57131bc5e49959344a4d7d97960"

PARKED_TOOL = "sessions__set_parked_session_priority"
PARKED_OP = "set_parked_session_priority_v1_workspaces__workspace_id__sessions__session_id__priority_put"
MODEL_SETTINGS_TOOL = "llm_providers__get_llm_provider_model_settings"
MODEL_SETTINGS_OP = "get_llm_provider_model_settings_v1_llm_providers__provider_id__model_settings_get"
IDENTITIES_408_SHA256 = "24df669d34275864bd36cdef528915c306f6d7b6caf2ad6ee306a53c4874c9a3"
IDENTITIES_409_SHA256 = "b4993ba3670a0782e5df1afa72b89cb396b31b528c6ecd8fbc565d3e9d7c259e"
TOOLS_493_SHA256 = "e2259c2b01de2c2698acd4b8181ad7086639e233dde1f838d312c1e04f0453ba"
TOOLS_494_SHA256 = "f0ca47325cb1d4ede7484b096dc6da190c1b498b5918629b363bcffafe5e6d07"
PRE_INSPECT_TOOLS_492_SHA256 = "d89346aa99e7b2707e5009969e20000f5e1484333313dcae99671f105fd6cb1a"
GENERATED_265_SHA256 = "e48ba071de262ffc3a6e9cb30c681592a361b366adb9b96aff7eb500e7d99bdd"
GENERATED_266_SHA256 = "6857dc30b6f2ec356f211d49a0fab8e51e96fd531b8057bdafafebd523e714cf"

# Frozen digests (sha256 over canonical JSON of the sorted values).
IDENTITIES_407_SHA256 = "495473987d4a6db1d31f5fc7f4b08791ecbc8e3bd7b673cb02e5f6162916862b"
PRE_PARKED_TOOLS_492_SHA256 = "6a3a0e67b7dbadc2f6917f885435eaab5dd14d268b5a5e07df79343d4558d59b"
PRE_INSPECT_TOOLS_491_SHA256 = "ef5c4ee79ee60787daaac54cebeebd3d27f8b6a1bee371f290c230c466ad2695"
PRE_PUBLISHER_TOOLS_490_SHA256 = "92c82e578e8e345ebcfbbf3930e6425f96b1e51bd6e2f2b5597809ce7316e9f7"
GENERATED_264_SHA256 = "1aa65d250b41f173af758dfb2a2b5f36c7d8d80d5dbaead5b15e219c0e081cac"
CURATED_PAIRS_135_SHA256 = "c4e02bffd013b8b430467a2ac8c5a6132cd6fd86d4c70ac82861524f58172d7e"
SKIPS_8_SHA256 = "48dce3e34e1e74800dfba42fb9130d43d0bc0b75d0387c7bd252ef7622099abf"
EMPTY_SET_SHA256 = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
REVIEWED_SKIP_SHA256 = "1db0ea6f6f63eb30bd781f84e212dd9c3829fe48af05fbecde59ad80f288ffe1"


def _digest(items: Sequence[object]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(items), separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="module")
def built_app():
    return create_app(AppConfig())


@pytest.fixture(scope="module")
def projection(built_app) -> tuple:
    return coverage_projection(built_app)


@pytest.fixture(scope="module")
def live_catalogue(built_app):
    # Build first so inventory enumerates this app's generated providers.
    assert len(built_app.state.generated_route_toolsets.operations) == 266
    return asyncio.run(inventory.build_builtin_catalogue())


def test_operation_identity_is_frozen(projection) -> None:
    rows = list(projection)
    assert len(rows) == 409
    assert len({(row.operation_id, row.method, row.path) for row in rows}) == 409
    identities = [[row.operation_id, row.method, row.path] for row in rows]
    assert _digest(identities) == IDENTITIES_409_SHA256
    assert _digest([item for item in identities if item[0] != MODEL_SETTINGS_OP]) == IDENTITIES_408_SHA256
    assert _digest(
        [item for item in identities if item[0] not in {MODEL_SETTINGS_OP, PARKED_OP}]
    ) == IDENTITIES_407_SHA256


def test_the_stable_tool_id_set_is_frozen(manifest, live_catalogue) -> None:
    ids = [tool.scoped_id for tool in live_catalogue]
    assert len(ids) == len(set(ids)) == 494
    assert all(item.count("__") == 1 for item in ids)
    assert _digest(ids) == TOOLS_494_SHA256
    inspect = "system__inspect_service_bundle"
    assert {PARKED_TOOL, inspect, "system__publish_service", MODEL_SETTINGS_TOOL} <= set(ids)
    # Each addition is exactly one id: removing it reproduces the frozen set it was added to.
    assert _digest([item for item in ids if item != MODEL_SETTINGS_TOOL]) == TOOLS_493_SHA256
    before_model_settings = [item for item in ids if item != MODEL_SETTINGS_TOOL]
    assert _digest([item for item in before_model_settings if item != inspect]) == PRE_INSPECT_TOOLS_492_SHA256
    assert _digest([item for item in before_model_settings if item != PARKED_TOOL]) == PRE_PARKED_TOOLS_492_SHA256
    previous_ids = [item for item in before_model_settings if item not in {PARKED_TOOL, inspect}]
    assert _digest(previous_ids) == PRE_INSPECT_TOOLS_491_SHA256
    assert _digest([item for item in previous_ids if item != "system__publish_service"]) == PRE_PUBLISHER_TOOLS_490_SHA256
    assert set(ids) == {tool["scoped_id"] for tool in manifest["tool_catalogue"]}
    assert next(tool for tool in manifest["tool_catalogue"] if tool["scoped_id"] == "system__publish_service") == {
        "bare_id": "publish_service", "exposable": False, "exposure_reason": "needs_workspace",
        "required_role": "admin", "scoped_id": "system__publish_service", "toolset_id": "system",
    }


def test_the_generated_declared_ids_are_frozen(projection, built_app) -> None:
    generated = [row for row in projection if row.state == "generated"]
    assert len(generated) == 266
    ids = [row.tool_ids[0] for row in generated]
    assert len(set(ids)) == 266
    assert _digest(ids) == GENERATED_266_SHA256
    assert _digest([item for item in ids if item != MODEL_SETTINGS_TOOL]) == GENERATED_265_SHA256
    assert _digest(
        [item for item in ids if item not in {MODEL_SETTINGS_TOOL, PARKED_TOOL}]
    ) == GENERATED_264_SHA256
    # Origin is descriptor-owned and must retain the same operation coordinates.
    by_operation = {row.operation_id: row for row in generated}
    for operation in built_app.state.generated_route_toolsets.operations:
        row = by_operation[operation.operation_id]
        assert operation.tool.origin == f"{row.method} {row.path}"
        assert operation.tool.operation == row.operation_id


def test_the_curated_pairs_are_frozen(projection) -> None:
    curated = [row for row in projection if row.state == "curated"]
    assert len(curated) == 135
    pairs = [[row.operation_id, list(row.tool_ids)] for row in curated]
    # 144 id declarations: 126 single + 9 one-to-many.
    assert sum(len(pair[1]) for pair in pairs) == 144
    assert sum(1 for pair in pairs if len(pair[1]) == 2) == 9
    assert _digest(pairs) == CURATED_PAIRS_135_SHA256


def test_the_skip_set_and_its_reviewed_digest_are_frozen(projection, manifest) -> None:
    skips = [row for row in projection if row.state == "skip"]
    assert len(skips) == 8
    assert all(row.skip_code == "human_sign_in" for row in skips)
    assert _digest([[row.method, row.path, row.skip_code] for row in skips]) == SKIPS_8_SHA256
    # The manifest's reviewed digest is recomputed over the same identities.
    manifest_skips = [row for row in manifest["rows"] if row["state"] == "skip"]
    assert inventory._digest_identities(manifest_skips) == REVIEWED_SKIP_SHA256
    assert manifest["reviewed_skip_sha256"] == REVIEWED_SKIP_SHA256
    # The projection's skip identities are the reviewed ones.
    assert {
        (row.operation_id, row.method, row.path) for row in skips
    } == {
        (row["operation_id"], row["method"], row["path"]) for row in manifest_skips
    }


def test_the_aggregation_set_is_frozen_empty(projection) -> None:
    """No tool maps more than one operation: the aggregation set stays the empty set."""
    aggregations = {row.aggregation_id for row in projection if row.aggregation_id is not None}
    assert aggregations == frozenset()
    assert _digest(list(aggregations)) == EMPTY_SET_SHA256


def test_the_manifest_baseline_is_frozen(manifest) -> None:
    assert manifest["schema_version"] == 1
    assert manifest["base_commit"] == BASE_COMMIT
    assert manifest["reviewed_surface_revision"] == "parked-session-priority-v1"
    assert manifest["expected_operation_count"] == 409
    assert manifest["expected_path_count"] == 278


def test_every_tool_has_exactly_one_reference_path(live_catalogue) -> None:
    """Reference-path freeze: unique reference/tools/<toolset>/<tool> for all 494."""
    paths = []
    for tool in live_catalogue:
        path = reference_path(tool.toolset_id, tool.bare_id)
        assert re.fullmatch(r"reference/tools/[^/]+/[^/]+", path)
        assert tool.scoped_id == f"{tool.toolset_id}__{tool.bare_id}"
        paths.append(path)
    assert len(set(paths)) == 494
