"""Workspace environment reservations are visible in generated schemas."""

from primer.api.routers.workspaces import WorkspaceCreateBody
from primer.model.workspace import WorkspaceTemplate


def test_workspace_template_env_schema_names_every_reserved_runtime_variable() -> None:
    description = WorkspaceTemplate.model_json_schema()["properties"]["env"]["description"]
    assert description == (
        "Environment variables injected for every shell session. PRIMER_RUNTIME_TOKEN and RUNTIME_TOKEN are reserved "
        "for the runtime token; WORKSPACE_ROOT is reserved for the runtime root. Set backend.workdir to control the root."
    )


def test_workspace_create_openapi_defs_describe_override_env_and_root_control() -> None:
    schema = WorkspaceCreateBody.model_json_schema()
    assert schema["properties"]["overrides"]["description"] == (
        "Optional per-instantiation overrides (environment variables, extra files, and additional init commands). "
        "Environment overrides cannot set PRIMER_RUNTIME_TOKEN, RUNTIME_TOKEN, or WORKSPACE_ROOT; set the template's "
        "backend.workdir to control the runtime root."
    )
    assert schema["$defs"]["WorkspaceTemplateOverrides"]["properties"]["env"]["description"] == (
        "Environment variables overlaid on the template for this workspace. PRIMER_RUNTIME_TOKEN and RUNTIME_TOKEN are "
        "reserved for the runtime token; WORKSPACE_ROOT is reserved for the runtime root. Set backend.workdir on the "
        "template to control the root."
    )


def test_workspace_openapi_describes_reserved_env_and_root_control(app) -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["WorkspaceTemplate"]["properties"]["env"]["description"] == (
        "Environment variables injected for every shell session. PRIMER_RUNTIME_TOKEN and RUNTIME_TOKEN are reserved "
        "for the runtime token; WORKSPACE_ROOT is reserved for the runtime root. Set backend.workdir to control the root."
    )
    assert schemas["WorkspaceTemplateOverrides"]["properties"]["env"]["description"] == (
        "Environment variables overlaid on the template for this workspace. PRIMER_RUNTIME_TOKEN and RUNTIME_TOKEN are "
        "reserved for the runtime token; WORKSPACE_ROOT is reserved for the runtime root. Set backend.workdir on the "
        "template to control the root."
    )
    assert schemas["WorkspaceCreateBody"]["properties"]["overrides"]["description"] == (
        "Optional per-instantiation overrides (environment variables, extra files, and additional init commands). "
        "Environment overrides cannot set PRIMER_RUNTIME_TOKEN, RUNTIME_TOKEN, or WORKSPACE_ROOT; set the template's "
        "backend.workdir to control the runtime root."
    )
