"""Unit tests for the Service / ServiceVersion entities (spec section 4)."""

import pytest
from pydantic import ValidationError

from primer.model.service import (
    RESERVED_SERVICE_NAMES,
    Service,
    ServiceManifest,
    ServiceToolGrant,
    ServiceVersion,
    ServiceViewerAuth,
)


def test_service_defaults() -> None:
    s = Service(name="status-page", description="the status page")
    assert s.id.startswith("service-")
    assert s.active_version_id is None
    assert s.viewer_auth is ServiceViewerAuth.CONSOLE
    assert s.harness_id is None


@pytest.mark.parametrize("bad", ["A", "-x", "UPPER", "a", "has space", "x" * 64, "_client"])
def test_service_slug_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        Service(name=bad, description="x")


@pytest.mark.parametrize("ok", ["ab", "status-page", "a1-b2", "x" * 63])
def test_service_slug_accepted(ok: str) -> None:
    assert Service(name=ok, description="x").name == ok


def test_reserved_names_include_client() -> None:
    assert "_client" in RESERVED_SERVICE_NAMES


def test_manifest_defaults_and_forbid() -> None:
    m = ServiceManifest()
    assert m.entry == "index.html"
    assert m.functions == [] and m.tools == []
    with pytest.raises(ValidationError):
        ServiceManifest(unknown_key=1)


def test_tool_grant_shape() -> None:
    g = ServiceToolGrant(toolset_id="ts-1")
    assert g.tool_names is None


def test_version_schema_alias() -> None:
    v = ServiceVersion(
        service_id="service-x",
        version=1,
        manifest=ServiceManifest(),
        files={"index.html": "artifact-1"},
        functions=[
            {
                "name": "run",
                "schema": {"type": "object"},
                "timeout_seconds": 30.0,
                "source_file": "functions.py",
            }
        ],
    )
    assert v.functions[0].schema_ == {"type": "object"}
    assert v.model_dump(by_alias=True)["functions"][0]["schema"] == {"type": "object"}
