"""Focused contract tests for the Tools Universal M0 inventory."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from scripts import tools_universal_inventory as inventory


MANIFEST_PATH = Path("scripts/data/tools_universal_m0_manifest.json")


@pytest.fixture(scope="module")
def current_inventory():
    """Build the app and reserved provider catalogue exactly once."""
    operations, path_count = inventory.build_openapi_inventory()
    tools = asyncio.run(inventory.build_builtin_catalogue())
    manifest = json.loads(MANIFEST_PATH.read_text())
    rows = inventory.validate_manifest(manifest, operations, path_count, tools)
    return manifest, operations, path_count, tools, rows


def test_baseline_pins_the_complete_reviewed_operation_universe(
    current_inventory,
) -> None:
    manifest, operations, path_count, tools, rows = current_inventory

    assert path_count == inventory.EXPECTED_PATH_COUNT == 273
    assert len(operations) == len(rows) == inventory.EXPECTED_OPERATION_COUNT == 404
    assert len({row["operation_id"] for row in rows}) == 404
    assert len({inventory._identity(row) for row in rows}) == 404
    assert len(tools) == len({tool.scoped_id for tool in tools}) == 226
    assert {row["state"] for row in rows} == {"curated", "gap", "skip"}
    assert manifest["base_commit"] == inventory.BASE_COMMIT


def test_gap_and_skip_sets_are_exactly_reviewed(current_inventory) -> None:
    manifest, _operations, _path_count, _tools, rows = current_inventory
    gaps = [row for row in rows if row["state"] == "gap"]
    skips = [row for row in rows if row["state"] == "skip"]

    assert len(gaps) == 261
    assert len(skips) == 8
    assert inventory._digest_identities(gaps) == manifest["reviewed_gap_sha256"]
    assert inventory._digest_identities(skips) == manifest["reviewed_skip_sha256"]
    assert {
        (row["method"], row["path"]) for row in skips
    } == inventory.HUMAN_SIGN_IN_PATHS
    assert {row["skip_code"] for row in skips} == {"human_sign_in"}
    assert all(row["source"] == "DESIGN.md#approved-decisions-2" for row in skips)

    internal_collection_gaps = {
        row["operation_id"]
        for row in gaps
        if row["path"].startswith("/v1/internal_collections")
    }
    assert internal_collection_gaps == {
        "bootstrap_status_v1_internal_collections_bootstrap_status_get",
        "bootstrap_v1_internal_collections_bootstrap_post",
        "delete_config_v1_internal_collections_config_delete",
        "get_config_v1_internal_collections_config_get",
        "put_config_v1_internal_collections_config_put",
    }


def test_strict_mode_is_deliberately_red_and_bounded(current_inventory) -> None:
    _manifest, _operations, _path_count, _tools, rows = current_inventory
    failure = inventory.strict_gap_error(rows)

    assert failure is not None
    assert failure.startswith("strict zero-gap check failed: 261 GAP operations")
    assert "internal-collections=5" in failure
    assert "bootstrap_v1_internal_collections_bootstrap_post" in failure
    assert len(failure) < 2_000


def test_evidence_rendering_is_deterministic(current_inventory, tmp_path: Path) -> None:
    _manifest, _operations, _path_count, tools, rows = current_inventory
    first = tmp_path / "first"
    second = tmp_path / "second"

    inventory.write_evidence(first, rows, tools)
    inventory.write_evidence(second, rows, tools)

    for name in ("inventory.json", "inventory.csv", "inventory.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
        assert (first / name).stat().st_size > 100


@pytest.mark.parametrize(
    "mutator, expected",
    [
        (lambda manifest: manifest.update({"rows": []}), "missing or vacuous"),
        (
            lambda manifest: manifest["rows"].append(
                copy.deepcopy(manifest["rows"][0])
            ),
            "manifest contains duplicate",
        ),
        (
            lambda manifest: manifest["rows"][0].update(
                {"route_source": "drifted:handler"}
            ),
            "route metadata drift",
        ),
        (
            lambda manifest: manifest["rows"][0].update(
                {"state": "curated", "tool_ids": ["unknown__tool"]}
            ),
            "unknown tool ids",
        ),
        (
            lambda manifest: manifest["rows"][0].update(
                {"state": "skip", "skip_code": "maintenance"}
            ),
            "unsupported skip classification",
        ),
    ],
)
def test_baseline_rejects_vacuity_duplicates_unknown_ids_and_drift(
    current_inventory,
    mutator,
    expected: str,
) -> None:
    manifest, operations, path_count, tools, _rows = current_inventory
    changed = copy.deepcopy(manifest)
    mutator(changed)

    with pytest.raises(inventory.InventoryError, match=expected):
        inventory.validate_manifest(changed, operations, path_count, tools)


def test_unreviewed_new_gap_fails_the_pinned_digest(current_inventory) -> None:
    manifest, operations, path_count, tools, _rows = current_inventory
    changed = copy.deepcopy(manifest)
    curated = next(row for row in changed["rows"] if row["state"] == "curated")
    curated.update(
        {
            "aggregation_id": None,
            "reason": "newly unreviewed gap",
            "skip_code": None,
            "source": "reviewed-gap:test",
            "state": "gap",
            "tool_ids": [],
        }
    )

    with pytest.raises(
        inventory.InventoryError, match="reviewed GAP manifest digest drift"
    ):
        inventory.validate_manifest(changed, operations, path_count, tools)


def test_many_operations_to_one_tool_requires_explicit_aggregation(
    current_inventory,
) -> None:
    manifest, operations, path_count, tools, _rows = current_inventory
    changed = copy.deepcopy(manifest)
    unique_mapping = next(
        row
        for row in changed["rows"]
        if row["state"] == "curated"
        and row["aggregation_id"] is None
        and len(row["tool_ids"]) == 1
    )
    other = next(
        row
        for row in changed["rows"]
        if row["state"] == "curated"
        and row is not unique_mapping
        and row["aggregation_id"] is None
    )
    other["tool_ids"].append(unique_mapping["tool_ids"][0])

    with pytest.raises(
        inventory.InventoryError, match="without one explicit aggregation_id"
    ):
        inventory.validate_manifest(changed, operations, path_count, tools)


def test_refresh_preserves_reviewed_rows_and_never_infers_by_name(
    current_inventory,
) -> None:
    manifest, operations, path_count, tools, rows = current_inventory
    refreshed = inventory.bootstrap_manifest(operations, path_count, tools, manifest)
    coverage_fields = {
        "aggregation_id",
        "reason",
        "skip_code",
        "source",
        "state",
        "tool_ids",
    }
    assert [
        {field: row[field] for field in coverage_fields} for row in refreshed["rows"]
    ] == [{field: row[field] for field in coverage_fields} for row in rows]

    similar_unseen = {
        **operations[0],
        "operation_id": "create_document_similar_but_unreviewed",
        "method": "POST",
        "path": "/v1/review-probe/documents",
    }
    unseen = inventory.bootstrap_manifest([similar_unseen], 1, tools)
    assert unseen["rows"][0]["state"] == "gap"
    assert unseen["rows"][0]["tool_ids"] == []


def test_semantic_corrections_and_remaining_multi_tool_rows_are_pinned(
    current_inventory,
) -> None:
    _manifest, _operations, _path_count, _tools, rows = current_inventory
    by_id = {row["operation_id"]: row for row in rows}
    expected_tools = {
        "_create_v1_documents_post": ["system__create_document"],
        "_delete_v1_documents__entity_id__delete": ["system__delete_document"],
        "_update_v1_documents__entity_id__put": [],
        "_list_v1_documents_get": [],
        "create_doc_v1_collections__collection_id__docs_post": [
            "collections__create_document"
        ],
        "delete_doc_v1_collections__collection_id__docs_delete": [
            "collections__delete_document"
        ],
        "find_sessions_v1_sessions_find_post": [],
        "find_workspaces_v1_workspaces_find_post": [],
        "file_tree_v1_workspaces__workspace_id__files_tree_get": [],
        "get_session_by_id_v1_sessions__session_id__get": [],
        "list_files_v1_workspaces__workspace_id__files_get": [
            "workspaces__list_workspace_files"
        ],
        "list_sessions_v1_sessions_get": [],
        "patch_doc_v1_collections__collection_id__docs_patch": [
            "collections__update_document"
        ],
        "put_collection_document_v1_collections__collection_id__documents_put": [
            "system__put_document"
        ],
        "move_collection_document_v1_collections__collection_id__documents_move_post": [
            "system__move_document"
        ],
        "move_doc_v1_collections__collection_id__docs_move_post": [
            "collections__move_document"
        ],
        "get_or_list_collection_documents_v1_collections__collection_id__documents_get": [
            "system__get_document_content",
            "system__list_documents",
        ],
        "get_or_list_docs_v1_collections__collection_id__docs_get": [
            "collections__collection_tree",
            "collections__read_document",
        ],
        "search_collection_v1_collections__collection_id__search_post": [
            "collections__search",
            "system__search_collection",
        ],
    }
    assert {
        operation_id: by_id[operation_id]["tool_ids"] for operation_id in expected_tools
    } == expected_tools

    assert {row["operation_id"] for row in rows if len(row["tool_ids"]) > 1} == {
        "_create_v1_agents_post",
        "_create_v1_graphs_post",
        "_update_v1_agents__entity_id__put",
        "_update_v1_graphs__entity_id__put",
        "create_trigger_endpoint_v1_triggers_post",
        "get_or_list_collection_documents_v1_collections__collection_id__documents_get",
        "get_or_list_docs_v1_collections__collection_id__docs_get",
        "search_collection_v1_collections__collection_id__search_post",
        "update_trigger_endpoint_v1_triggers__trigger_id__put",
    }
    assert all(row["aggregation_id"] is None for row in rows)
