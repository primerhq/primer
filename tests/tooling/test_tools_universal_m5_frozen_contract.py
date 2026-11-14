"""M5 frozen contract: the reviewed Tools Universal universe, digested.

Restamped for W3 welcome (base ``6fe6f03d7``): exactly two new generated
operations; curated/skip/aggregation/core-kit contracts are unchanged. From the live
production projection and the reviewed manifest:

* 406 operation identities (operationId, method, path)
* 489 scoped tool ids (exact set, from the reviewed tool catalogue)
* 263 generated declared ids
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

# Frozen digests (sha256 over canonical JSON of the sorted values).
IDENTITIES_406_SHA256 = "0fca73ea3dceb3f52eeab4d623770e0314327c66238eadf6cba7b9003ffd26c6"
TOOLS_489_SHA256 = "8628a4645190ebe17a07d32bdbcbf884411ba91d89c669408aaa7c1001235297"
GENERATED_263_SHA256 = "bba67fb59cfb337d44f66c7d8f5ad9dda5bdf0b93806b494e44c5a1d9d841b7e"
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
    assert len(built_app.state.generated_route_toolsets.operations) == 263
    return asyncio.run(inventory.build_builtin_catalogue())


def test_operation_identity_is_frozen(projection) -> None:
    rows = list(projection)
    assert len(rows) == 406
    assert len({(row.operation_id, row.method, row.path) for row in rows}) == 406
    assert _digest([[row.operation_id, row.method, row.path] for row in rows]) == IDENTITIES_406_SHA256


def test_the_stable_tool_id_set_is_frozen(manifest, live_catalogue) -> None:
    ids = [tool.scoped_id for tool in live_catalogue]
    assert len(ids) == len(set(ids)) == 489
    assert all(item.count("__") == 1 for item in ids)
    assert _digest(ids) == TOOLS_489_SHA256
    assert set(ids) == {tool["scoped_id"] for tool in manifest["tool_catalogue"]}


def test_the_generated_declared_ids_are_frozen(projection, built_app) -> None:
    generated = [row for row in projection if row.state == "generated"]
    assert len(generated) == 263
    ids = [row.tool_ids[0] for row in generated]
    assert len(set(ids)) == 263
    assert _digest(ids) == GENERATED_263_SHA256
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
    assert manifest["reviewed_surface_revision"] == "wiki-first-w3-welcome"
    assert manifest["expected_operation_count"] == 406
    assert manifest["expected_path_count"] == 275


def test_every_tool_has_exactly_one_reference_path(live_catalogue) -> None:
    """Reference-path freeze: unique reference/tools/<toolset>/<tool> for all 489."""
    paths = []
    for tool in live_catalogue:
        path = reference_path(tool.toolset_id, tool.bare_id)
        assert re.fullmatch(r"reference/tools/[^/]+/[^/]+", path)
        assert tool.scoped_id == f"{tool.toolset_id}__{tool.bare_id}"
        paths.append(path)
    assert len(set(paths)) == 489
