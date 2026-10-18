"""The console's built-in provider rows are the server's reserved ids (plan 02, Task 9; design 6.8).

The Register overlay and the Providers page offer no Delete on a built-in row and no Edit on a read-only one: the server answers 403 to both. The console
keeps ONE copy of those ids (``PC_BUILTIN_IDS`` and ``PC_READONLY_IDS`` in ``ui/components/providers/register-common.jsx``), and this file holds that copy to
the server's own constants, so a reserved id added, renamed or dropped on the server fails here instead of offering a Delete the server refuses (or hiding
one it allows). ``tests/ui/test_provider_catalog_actions.py::test_no_reserved_id_list_is_duplicated_in_the_console`` keeps the catalog itself free of a copy.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.ui._register_harness import COMPONENTS, transpile


def _console(name: str):
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval("var window = globalThis; var React = {};")
        ctx.eval(transpile(COMPONENTS / "providers" / "register-common.jsx"))
        return json.loads(ctx.eval(f"JSON.stringify({name})"))
    finally:
        ctx.close()


def test_console_built_ins_equal_the_server_reserved_ids() -> None:
    from primer.api.registries.provider_registry import (
        RESERVED_CROSS_ENCODER_IDS,
        RESERVED_EMBEDDER_IDS,
        RESERVED_LLM_IDS,
        RESERVED_SSP_IDS,
        RESERVED_WORKSPACE_PROVIDER_IDS,
    )
    from primer.artifact.factory import DEFAULT_ARTIFACT_PROVIDER_ID
    from primer.model.web_fetch import RESERVED_WEB_FETCH_IDS
    from primer.model.web_search import RESERVED_WEB_SEARCH_IDS

    server = {
        "embedding": RESERVED_EMBEDDER_IDS,
        "ssp": RESERVED_SSP_IDS,
        "cross_encoder": RESERVED_CROSS_ENCODER_IDS,
        "workspace": RESERVED_WORKSPACE_PROVIDER_IDS,
        "web_search": RESERVED_WEB_SEARCH_IDS,
        "web_fetch": RESERVED_WEB_FETCH_IDS,
        "artifact_storage": frozenset({DEFAULT_ARTIFACT_PROVIDER_ID}),
    }
    console = _console("PC_BUILTIN_IDS")
    assert {key: set(ids) for key, ids in console.items()} == {key: set(ids) for key, ids in server.items()}
    assert all(len(ids) == len(set(ids)) for ids in console.values()), "an id listed twice"
    assert RESERVED_LLM_IDS == frozenset() and "llm" not in console, "no LLM row is built in, and the console says none is"


def test_read_only_ids_are_the_rows_whose_put_is_refused() -> None:
    """Only the local workspace provider refuses a PUT (403, ``primer/api/routers/workspaces.py``); every other built-in row can still be saved."""
    from primer.api.registries.provider_registry import RESERVED_WORKSPACE_PROVIDER_IDS
    from primer.api.routers.workspaces import _reject_reserved_workspace_provider_update

    readonly = _console("PC_READONLY_IDS")
    assert readonly == {"workspace": sorted(RESERVED_WORKSPACE_PROVIDER_IDS)}
    for rid in readonly["workspace"]:
        with pytest.raises(HTTPException) as refused:
            asyncio.run(_reject_reserved_workspace_provider_update(SimpleNamespace(id=rid), SimpleNamespace(id=rid), None))
        assert refused.value.status_code == 403
        assert refused.value.detail["error"] == "reserved_id_protected"
