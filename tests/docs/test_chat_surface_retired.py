"""The user-facing chat surface is retired (S1 P7 exit gate).

Amendment M8 scopes this gate to ENGINE IMPORTS, API ROUTES and the
DELETED MODELS. It is deliberately NOT a whole-tree "the word chat does
not appear" sweep: the C4 carve-out keeps the headless chat engine alive
for channels until S6 P5 replaces it with thread-mapped sessions, so the
word necessarily still appears and a maximal gate would be unsatisfiable
by construction.

What the carve-out deliberately KEEPS (see crosscheck C4):
    primer/model/chats.py
    primer/chat/{dispatch,executor,enqueue,pending,tick_router,usage_cache}.py
    the CHAT claim lane (int/claim.py, claim/adapters/chats.py, ...)
    primer/trigger/subscribers/{chat_message,start_chat}.py
    primer/channel/{chat_inbox,chat_router,chat_dispatcher}.py
Those files carry an "S6 P5 deletes this file" docstring stamp, which
tests/docs/test_chat_carveout_ownership.py enforces separately.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRIMER = ROOT / "primer"

# Modules P7 deleted. Nothing under primer/ may import them, and they
# must not be importable at all.
RETIRED_MODULES = (
    "primer.api.routers.chats",
    "primer.chat.rewind",
    "primer.model.thread",
)


def test_no_module_imports_a_retired_module() -> None:
    """Assertion 1: no live import of a deleted module."""
    offenders: list[str] = []
    for path in sorted(PRIMER.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for mod in RETIRED_MODULES:
            # `import x.y` / `from x.y import z` only - a prose mention
            # inside a docstring is not an import and does not break.
            for form in (f"import {mod}", f"from {mod} import"):
                if form in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {form}")
    assert not offenders, "retired modules are still imported:\n" + "\n".join(offenders)


def test_the_retired_modules_are_unimportable() -> None:
    """Assertion 3: the symbols are gone, not merely unreferenced."""
    for mod in RETIRED_MODULES:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod)


def test_the_app_exposes_no_chat_routes() -> None:
    """Assertion 2: behavioural, not textual.

    Resolve through ``app.openapi()`` rather than walking ``app.routes``:
    includes are deferred as ``_IncludedRouter`` objects that carry no
    ``.path`` until the schema is built, so a naive walk finds nothing
    and passes vacuously. Building the schema is what actually catches a
    chat route living in some OTHER router - which is exactly how
    /v1/chats/{chat_id}/tool_approval/pending and its external_tools
    sibling outlived the chats router itself.
    """
    from fastapi import FastAPI

    from primer.api._app_routes import _mount_routers

    app = FastAPI()
    _mount_routers(app)
    paths = app.openapi().get("paths", {})
    assert len(paths) > 100, (
        f"only {len(paths)} paths resolved; the gate would pass vacuously"
    )
    chat_routes = [p for p in paths if p.startswith("/v1/chats")]
    assert not chat_routes, f"chat routes still mounted: {chat_routes}"
