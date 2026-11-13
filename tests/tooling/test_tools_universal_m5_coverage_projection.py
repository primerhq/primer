"""M5 coverage projection: the production zero-gap reader, frozen against the reviewed manifest.

``coverage_projection`` derives every operation's state and tool ids from
route metadata plus the compiled generated snapshot. This module pins that
the projection equals the reviewed M0 manifest row-for-row (404/404), that
the build is deterministic, that no ``gap`` row exists, and that no
production module may import the inventory script or read the manifest.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from fastapi import FastAPI

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.api.route_tools import build_generated_route_toolsets, coverage_projection, route_tool
from primer.model.scheduler import RuntimeMode
from scripts import tools_universal_inventory as inventory

MANIFEST_PATH = Path("scripts/data/tools_universal_m0_manifest.json")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="module")
def default_app() -> object:
    """One full production build (default runtime mode) for the module."""
    return create_app(AppConfig())


@pytest.fixture(scope="module")
def projection(default_app) -> tuple:
    return coverage_projection(default_app)


def test_projection_equals_the_manifest_row_for_row(projection, manifest) -> None:
    """404/404 on identity, state, canonical ids, aggregation, skip and source."""
    rows = list(projection)
    assert len(rows) == 404
    assert len({row.operation_id for row in rows}) == 404
    by_op = {row.operation_id: row for row in rows}
    for man in manifest["rows"]:
        row = by_op[man["operation_id"]]
        assert (row.method, row.path) == (man["method"], man["path"]), man["operation_id"]
        assert row.state == man["state"], man["operation_id"]
        assert row.aggregation_id == man.get("aggregation_id"), man["operation_id"]
        assert row.skip_code == man.get("skip_code"), man["operation_id"]
        assert row.route_source == man["route_source"], man["operation_id"]
        if man["state"] == "generated":
            assert row.tool_ids == (man["declared_generated_tool_id"],), man["operation_id"]
        elif man["state"] == "curated":
            assert row.tool_ids == tuple(sorted(man["tool_ids"])), man["operation_id"]
        else:
            assert row.tool_ids == ()
    assert set(by_op) == {man["operation_id"] for man in manifest["rows"]}


def test_the_script_side_classification_fields_are_pinned(manifest) -> None:
    """mount_auth / rbac_class stay script-side; their reviewed distributions are frozen.

    Decision 3 keeps ``mount_auth``/``rbac_class`` out of the production
    projection; the projection is compared against the manifest, so the
    manifest's own classification must stay exactly as reviewed (section 1).
    """
    rows = manifest["rows"]
    assert Counter(row["mount_auth"] for row in rows) == Counter(
        {"admin": 154, "user": 211, "settings-read": 21, "public": 16, "authenticated": 2}
    )
    assert Counter(row["rbac_class"] for row in rows) == Counter(
        {"administrator": 154, "user": 204, "global-settings-read": 21, "human-auth": 7, "authenticated-account": 9, "public": 9}
    )
    generated = [row for row in rows if row["state"] == "generated"]
    assert all(row["declared_generated_tool_id"] for row in generated)
    curated = [row for row in rows if row["state"] == "curated"]
    assert all(row["declared_generated_tool_id"] is None for row in curated)


def test_the_projection_is_deterministic_across_builds(default_app, manifest, projection) -> None:
    """Two builds give byte-identical projections (pattern: inventory stability test)."""
    del default_app  # the module fixture built the default app first
    api_app = create_app(AppConfig(runtime_mode=RuntimeMode.API))
    first = coverage_projection(api_app)
    second = coverage_projection(api_app)
    assert first == second
    # Same content as the default-mode build: identity, state and ids are mode-independent.
    assert [
        (row.operation_id, row.method, row.path, row.state, row.tool_ids, row.skip_code, row.route_source)
        for row in first
    ] == [
        (row.operation_id, row.method, row.path, row.state, row.tool_ids, row.skip_code, row.route_source)
        for row in projection
    ]
    assert len(first) == 404


def test_zero_gap_rows(projection) -> None:
    """The production zero-gap invariant: an unannotated operation would be a gap row."""
    gaps = [row for row in projection if row.state == "gap"]
    assert gaps == []


def test_projection_rows_are_immutable_and_cached_per_app(default_app, projection) -> None:
    assert isinstance(projection, tuple)
    assert all(isinstance(row.tool_ids, tuple) for row in projection)
    assert projection is coverage_projection(default_app)
    with pytest.raises(FrozenInstanceError):
        projection[0].state = "gap"
    assert Counter(row.state for row in projection) == {
        "generated": 261, "curated": 135, "skip": 8,
    }


def test_generated_aggregation_metadata_is_not_silently_dropped() -> None:
    app = FastAPI()

    @app.get("/v1/m5-probes/aggregation")
    @route_tool(scoped_id="m5_probe__aggregation", aggregation_id="explicit-group")
    async def probe() -> dict:
        return {}

    build_generated_route_toolsets(app)
    row, = coverage_projection(app)
    assert row.state == "generated"
    assert row.aggregation_id == "explicit-group"


def test_no_production_module_imports_scripts_or_reads_the_manifest() -> None:
    """Static guard: production derives coverage without the script or manifest JSON."""
    root = Path("primer")
    offenders = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        if "tools_universal_m0_manifest" in source:
            offenders.append(f"{path}: manifest path literal")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import scripts") or stripped.startswith("from scripts"):
                offenders.append(f"{path}: {stripped}")
    assert offenders == [], "\n".join(offenders)
    # The script itself still owns the strict-gap contract the production reader mirrors.
    assert inventory.strict_gap_error([]) is None
