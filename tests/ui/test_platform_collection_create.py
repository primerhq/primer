"""Platform "New collection" hosts the collection dialog on the page (ADM-06 of the 2026-10-08 admin review, collections).

"New collection" on Platform > Collections opened the LEGACY Collections list overlay (a table with its own "New collection" button) and only a second press
opened the dialog. The page now hosts ``KN_NewCollectionModal`` (the dialog the legacy list used, unchanged) the way toolsets, triggers, services and
harnesses are hosted (``tests/ui/test_platform_create_opens_the_form.py`` tabulates what every page's create does).

One thing is specific to collections. The legacy list primes a module-scope cache, ``KN_justCreatedCache``, with the row the dialog created before it
addresses the new collection's detail overlay, because the overlay's own list fetch may not have caught up and the overlay remounts at exactly that moment
(``test_collections_create_addressing.py`` is the full story: it was the u0025 flake). A host that opened the overlay without priming that cache would
bring the flake back, so the host calls ``KN_rememberJustCreated`` first.

The page table and the hand-off are plain JS that runs here in MiniRacer; the host and the mount are JSX, so they are source checks (this checkout has no
render harness for them), and the real journey is ``tests/ui_e2e/test_platform_collection_create_journey.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PLAT = (UI / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
KNOWLEDGE = (UI / "components" / "knowledge.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def test_the_collections_create_sets_a_modal_and_opens_no_overlay() -> None:
    ctx = _ctx()
    ctx.eval("var window = globalThis;")
    ctx.eval(PLAT[PLAT.index("function NV_fact("):PLAT.index("var NV_PLAT_PAGE_SIZE")])
    ctx.eval(
        "var modals = [], overlays = [];"
        "var con = { openOverlay: function (a, b, c) { overlays.push([a, b === undefined ? null : b, c === undefined ? null : c]); } };"
        "function setModal(m) { modals.push(m); }"
    )

    ctx.eval('NV_PLAT_PAGES.collections.create(con, setModal);')

    assert _js(ctx, "modals") == [{"kind": "collection"}]
    assert _js(ctx, "overlays") == [], "a hosted dialog must not also open the legacy list"


def test_the_dialog_is_reachable_from_the_platform_page() -> None:
    assert "window.KN_NewCollectionModal = KN_NewCollectionModal;" in KNOWLEDGE


def test_the_platform_page_hosts_the_dialog_and_hands_the_created_row_on() -> None:
    host = re.search(r"function NV_CollectionCreateHost[\s\S]{0,700}", PLAT)
    assert host, "the page needs a host for the collection dialog"
    assert "window.KN_NewCollectionModal" in host.group(0)
    # The dialog toasts "Collection created" itself, but only through the pushToast it is given.
    assert re.search(r"pushToast=\{window\.primerApi\.toastPush\}", host.group(0)), "without pushToast nothing says the collection was created"

    shown = re.search(r"modal\.kind === \"collection\"[\s\S]{0,600}", PLAT)
    assert shown and "<NV_CollectionCreateHost" in shown.group(0)
    block = shown.group(0)
    assert block.count("setModal(null)") >= 2, "the dialog must close on Cancel and on a created row"
    assert re.search(r"NV_createdRow\(con, [\s\S]{0,60}\"collections\", row\)", block)


def test_the_host_primes_the_just_created_cache_before_it_hands_the_row_on() -> None:
    """Without it the detail overlay can open on a list that does not have the row yet (the u0025 flake)."""
    host = re.search(r"function NV_CollectionCreateHost[\s\S]{0,700}", PLAT).group(0)

    assert "window.KN_rememberJustCreated" in host
    assert host.index("KN_rememberJustCreated(") < host.index("props.onCreated(row)"), "the cache must be primed first"
    assert "window.KN_rememberJustCreated = KN_rememberJustCreated;" in KNOWLEDGE


def _cache_src() -> str:
    start = KNOWLEDGE.index("const KN_justCreatedCache = {};")
    fn = KNOWLEDGE.index("function KN_rememberJustCreated(")
    end = KNOWLEDGE.index("\n}\n", fn) + 3
    return KNOWLEDGE[start:end]


def test_remembering_a_created_row_stores_it_under_its_id() -> None:
    ctx = _ctx()
    ctx.eval(_cache_src())

    ctx.eval('KN_rememberJustCreated({ id: "wiki-1", description: "d" });')

    assert _js(ctx, "KN_justCreatedCache") == {"wiki-1": {"id": "wiki-1", "description": "d"}}


@pytest.mark.parametrize("row", ["null", "undefined", "{}", '{ id: "" }'])
def test_a_row_without_an_id_is_not_stored_under_the_word_undefined(row: str) -> None:
    ctx = _ctx()
    ctx.eval(_cache_src())

    ctx.eval(f"KN_rememberJustCreated({row});")

    assert _js(ctx, "Object.keys(KN_justCreatedCache)") == []
