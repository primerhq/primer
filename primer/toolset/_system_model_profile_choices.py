"""``system::list_model_profile_choices``: the tool twin of ``GET /v1/model_profiles/choices`` (audit G3-02).

The profile tools (``list_model_profiles``, ``get_model_profile`` and the writes) are admin-only, as their REST router, because a profile is
provider configuration. A user-role agent may still create or edit an agent (``create_agent`` / ``update_agent`` are user tier), and an agent
must name a profile in ``model.profile_id``, so this user-tier tool lists every profile by id, description, kind and context length, sorted by
id and paged like the other list tools, and nothing of the provider, the model name, the members or the config. It reads the rows through
:func:`primer.model_profile.choices.list_model_profile_choices`, the function the route answers from.

The storage is resolved inside the handler, not when the toolset is built: tests and the docs checks build the toolset over fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolExample
from primer.model.except_ import PrimerError
from primer.model.model_profile import ModelProfile
from primer.model.storage import CursorPage, OffsetPage
from primer.model_profile.choices import list_model_profile_choices
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok as _ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID, _err_from_primer, _err_from_validation

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider
    from primer.toolset.internal import ToolHandler


class _ListModelProfileChoicesArgs(BaseModel):
    """A page of the choices: ``offset`` or ``cursor``, never both. There is no ``order_by``: the choices are sorted by id."""

    limit: int = Field(default=200, ge=1, le=200, description="Maximum number of profiles returned (1-200, default 200).")
    offset: int | None = Field(
        default=None, ge=0,
        description="Number of profiles to skip. Mutually exclusive with ``cursor``; with neither, the list starts at the first profile.",
    )
    cursor: str | None = Field(
        default=None, description="The ``next_cursor`` a previous call returned. Mutually exclusive with ``offset``.",
    )


def _handler(storage_provider: StorageProvider) -> ToolHandler:
    async def handler(arguments: dict[str, Any]) -> Any:
        try:
            args = _ListModelProfileChoicesArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if args.offset is not None and args.cursor is not None:
            return _err("supply either ``offset`` or ``cursor``, not both", error_type="validation-error")
        page = CursorPage(cursor=args.cursor, length=args.limit) if args.cursor is not None else OffsetPage(
            offset=args.offset or 0, length=args.limit,
        )
        try:
            return _ok(await list_model_profile_choices(storage_provider.get_storage(ModelProfile), page))
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")

    return handler


def model_profile_choices_tools(*, storage_provider: StorageProvider) -> dict[str, tuple[Tool, ToolHandler]]:
    """The one tool, keyed by id, for ``build_system_toolset`` to add to the ``system`` registry."""
    tool = make_tool(
        id="list_model_profile_choices",
        toolset_id=SYSTEM_TOOLSET_ID,
        purpose=(
            "List the model profiles an agent may name, sorted by id: a page of ``{id, description, kind, context_length}``. ``kind`` is "
            "``single`` or ``aggregated``; ``context_length`` is the stored window in tokens, null for an aggregated profile. Never "
            "returns a profile's provider, model name, members or config."
        ),
        when=(
            "Use when you are not an admin and need the id to put in an agent's ``model.profile_id`` (``create_agent``, "
            "``update_agent``): ``list_model_profiles`` is admin-only because a profile is provider configuration. Not for reading "
            "a profile's provider or config (an admin uses ``get_model_profile``)."
        ),
        args_schema=_ListModelProfileChoicesArgs.model_json_schema(),
        examples=[
            ToolExample(
                args={},
                returns=(
                    '{"kind": "offset", "offset": 0, "length": 1, "total": 1, "items": [{"id": "lp-claude--claude-sonnet-4-6", '
                    '"description": "Claude Sonnet on the lp-claude provider", "kind": "single", "context_length": 200000}]}'
                ),
            ),
        ],
        required_role="user",
    )
    return {tool.id: (tool, _handler(storage_provider))}


__all__ = ["model_profile_choices_tools"]
