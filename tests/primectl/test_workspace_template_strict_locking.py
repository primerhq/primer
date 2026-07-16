"""Round-trip proof that ``strict_write_locking`` is usable through
primectl's generic, OpenAPI-schema-driven CRUD path.

primectl has no per-field CLI flags: ``crud.py`` builds every resource's
``create``/``apply``/``edit`` commands generically from the OpenAPI schema,
and ``--set field=value`` pairs are assembled into a JSON body via
``_assemble_set`` + ``coerce_value``. Once ``WorkspaceTemplate.strict_write_
locking`` (Task 9) lands on the model, it auto-appears in the OpenAPI
component schema and becomes settable as
``primectl create workspace_templates --set strict_write_locking=true``
with no bespoke flag machinery to add. This test proves both halves of
that claim: the ``--set`` coercion produces the right JSON value, and the
field is actually exposed in the live app's OpenAPI schema.
"""

from __future__ import annotations

from primectl.commands.crud import _assemble_set

from primer.api.app import create_app
from primer.api.config import AppConfig


def test_set_coerces_strict_write_locking_bool() -> None:
    """``--set strict_write_locking=true`` assembles to a JSON bool, not
    the string "true" - this is what a real ``primectl create
    workspace_templates --set strict_write_locking=true`` sends as the
    request body field."""
    body = _assemble_set(["strict_write_locking=true"])
    assert body["strict_write_locking"] is True

    body_false = _assemble_set(["strict_write_locking=false"])
    assert body_false["strict_write_locking"] is False


def test_schema_exposes_field_in_openapi() -> None:
    """The live app's OpenAPI schema carries ``strict_write_locking`` on
    the ``WorkspaceTemplate`` component - this is the schema primectl
    introspects to build its generic ``create``/``apply``/``edit``/
    ``describe`` commands, so the field round-trips with no per-field
    flag needed."""
    app = create_app(AppConfig())
    schema = app.openapi()
    comps = schema["components"]["schemas"]
    wt = comps["WorkspaceTemplate"]
    assert "strict_write_locking" in wt["properties"]
    assert wt["properties"]["strict_write_locking"]["type"] == "boolean"
