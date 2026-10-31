"""ACL-P3-T7: the console shares collections, triggers and harnesses, and reads
collections read-only (plan
docs/superpowers/plans/2026-10-31-access-control-p3-collections-triggers-harnesses.md,
Task 7; access spec 6.2 and 9, P3 decisions 4 and 5).

The backend owns the sharing: every row carries ``access``,
``GET /v1/{plural}/{id}/access`` answers ``{owners, grants, public,
public_tools, rev, your_level}``, a direct share of a harness-installed row
answers 409 code harness_managed, and a read-level save answers 403 code
access_denied. This file pins the CONSOLE side against that contract:

* ``ui/components/knowledge.jsx``: the collection detail is a shareable
  surface (a Share button, the seam subscription, the dialog with
  ``plural="collections"`` and no public-tools section), and the viewer's
  level from ``GET /collections/{id}/access`` gates the writes: at ``read``
  the document writes (new, import, edit, move, delete), the search toggle
  and the collection delete stay drawn but disabled with the read-only
  reason; at ``full`` the search toggle and the delete are disabled with the
  owner-only reason. The level decision is ``KN_writeGates``, run here in
  MiniRacer against the real source.
* ``ui/components/triggers.jsx`` / ``ui/components/harnesses.jsx``: the
  trigger and harness detail surfaces draw the Share button, subscribe to
  the share seam and open the dialog with their plural.
* ``ui/components/shared/share-dialog.jsx``: a harness-managed row (a
  non-null ``harness_id``) draws ``SHR_HarnessShare`` - "Shared through
  harness <id>", a button that opens the harness - INSTEAD of the Share
  button, on the agent, graph, toolset and collection detail surfaces.
* ``ui/foundation/api.js``: a 409/403 with code ``harness_managed`` renders
  through ``readRefusal`` with a human sentence.

The JSX wiring is source-checked against the real lines (the house style of
tests/ui/test_workspace_sharing_ui.py); the two pure decisions (the level
gates, the refusal sentence) run in MiniRacer against the real source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

INDEX = (UI / "index.html").read_text(encoding="utf-8")
KNOWLEDGE_SRC = (UI / "components" / "knowledge.jsx").read_text(encoding="utf-8")
TRIGGERS_SRC = (UI / "components" / "triggers.jsx").read_text(encoding="utf-8")
HARNESS_SRC = (UI / "components" / "harnesses.jsx").read_text(encoding="utf-8")
AGENTS_SRC = (UI / "components" / "agents.jsx").read_text(encoding="utf-8")
GRAPHS_SRC = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")
TOOLSETS_SRC = (UI / "components" / "toolsets.jsx").read_text(encoding="utf-8")
DIALOG_SRC = (UI / "components" / "shared" / "share-dialog.jsx").read_text(encoding="utf-8")
KNOWLEDGE_PATH = UI / "components" / "knowledge.jsx"
API_PATH = UI / "foundation" / "api.js"

_OPEN_CONTEXTS: list = []

RO_NOTE = "Read-only: you can view this collection"
OWNER_NOTE = "Only an owner can do this"


def _after(src: str, anchor: str, window: int = 300) -> str:
    i = src.index(anchor)
    return src[i : i + window]


def _transpiled(path: Path) -> str:
    return transpile(path)


def _mini_ctx(src: str):
    ctx = mini_react_context(src)
    _OPEN_CONTEXTS.append(ctx)
    return ctx


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


# ===========================================================================
# The shareable surfaces (collections, triggers, harnesses)
# ===========================================================================


def _shareable_surface(src: str, plural: str, testid: str) -> None:
    """The detail surface draws the Share button (the entity.share verb),
    subscribes to the seam so the palette's verb reaches it, and opens the
    dialog with its plural - no public-tools section (none of the three
    kinds has per-tool public exposure)."""
    btn = _after(src, f'data-testid="{testid}"', 260)
    assert 'data-verb="entity.share"' in btn, "the button is not the share verb"
    assert "window.SHR_useShareOpen" in src, "the seam subscription is missing"
    dlg = _after(src, "<SHR_ShareDialog", 400)
    assert f'plural="{plural}"' in dlg, f"the dialog must fetch /{plural}/{{id}}/access"
    assert "allowPublicTools" not in dlg, f"{plural} have no per-tool public switch"


def test_the_collection_detail_is_a_shareable_surface() -> None:
    _shareable_surface(KNOWLEDGE_SRC, "collections", "collection-share")


def test_the_trigger_detail_is_a_shareable_surface() -> None:
    _shareable_surface(TRIGGERS_SRC, "triggers", "trigger-share")


def test_the_harness_detail_is_a_shareable_surface() -> None:
    _shareable_surface(HARNESS_SRC, "harnesses", "harness-share")


def test_the_share_dialog_loads_before_the_p3_surfaces() -> None:
    """SHR_ShareDialog, SHR_HarnessShare and the seam globals are read at
    open time, so the dialog script must load before the three surfaces
    render (ui/index.html discipline: shared components before pages)."""
    dlg = INDEX.index('src="components/shared/share-dialog.jsx"')
    for page in ("knowledge.jsx", "triggers.jsx", "harnesses.jsx"):
        assert dlg < INDEX.index(f'src="components/{page}"'), (
            f"share-dialog.jsx must load before {page}"
        )


# ===========================================================================
# The harness-managed row (a non-null harness_id)
# ===========================================================================


def test_shr_harness_share_is_the_replacement_affordance() -> None:
    """The link to the harness: drawn instead of the Share button on a row
    the harness installed (the server answers 409 harness_managed to a
    direct share), and it opens the harness."""
    comp = _after(DIALOG_SRC, "function SHR_HarnessShare", 700)
    assert 'data-testid="shr-harness-share"' in comp, "the affordance carries no testid"
    assert "Shared through harness" in comp, "the sentence names the harness"
    assert "window.SHR_HarnessShare = SHR_HarnessShare" in DIALOG_SRC, (
        "the affordance is not exported for the detail surfaces"
    )


def test_harness_managed_rows_show_the_harness_instead_of_share() -> None:
    """All four kinds' detail surfaces: a non-null harness_id swaps the
    Share button for the harness link (agent, graph, toolset, collection)."""
    # agents.jsx: the edit form's footer (isManaged = the row's harness_id).
    ag = _after(AGENTS_SRC, "SHR_HarnessShare", 400)
    assert "harness_id" in _after(AGENTS_SRC, "isManaged", 120) or "harnessId" in ag, (
        "the agent surface does not branch on the row's harness_id"
    )
    # graphs.jsx: the status panel takes the id and opens the harness.
    assert "harnessId" in _after(GRAPHS_SRC, "SHR_HarnessShare", 400), (
        "the graph panel does not receive the harness id"
    )
    # toolsets.jsx: the detail actions take the id and open the harness.
    assert "harnessId" in _after(TOOLSETS_SRC, "SHR_HarnessShare", 400), (
        "the toolset actions do not receive the harness id"
    )
    # knowledge.jsx: the collection row's harness_id drives the swap.
    kn = _after(KNOWLEDGE_SRC, "SHR_HarnessShare", 400)
    assert "harness_id" in kn, "the collection surface does not branch on harness_id"


def test_harness_managed_opening_navigates_to_the_harness() -> None:
    """The affordance's click opens the harness detail, on every surface
    that can draw it (a trigger and a harness are not installed by a
    harness, so they never do)."""
    for src, name in (
        (KNOWLEDGE_SRC, "knowledge"),
        (AGENTS_SRC, "agents"),
        (GRAPHS_SRC, "graphs"),
        (TOOLSETS_SRC, "toolsets"),
    ):
        assert '"/harnesses/"' in src, f"{name}.jsx does not open the harness detail"


# ===========================================================================
# The read-only collection (the viewer's level from /collections/{id}/access)
# ===========================================================================


def test_the_collection_reads_its_access_level() -> None:
    """One read per open collection (useResource, key collection-access:<cid>),
    from the access sub-resource; the level is the view's your_level."""
    read = _after(KNOWLEDGE_SRC, '"collection-access:"', 500)
    assert "/access" in read, "the level must come from GET /collections/{id}/access"
    assert "your_level" in KNOWLEDGE_SRC, "the level is the view's your_level"


def test_the_level_gates_are_the_single_decision() -> None:
    """KN_writeGates against the real source: read disables every write;
    full loses the search toggle and the delete only; owner keeps them; a
    system collection is read-only to everyone (its row is public, so a
    non-owner level answers read); an unknown level (the read in flight)
    disables nothing - the server enforces the save."""
    ctx = _mini_ctx(_transpiled(KNOWLEDGE_PATH))

    def gates(level, system: bool) -> dict:
        return json.loads(
            ctx.eval(f"JSON.stringify(KN_writeGates({json.dumps(level)}, {json.dumps(system)}))")
        )

    assert gates(None, False) == {"ro": False, "ownerOnly": False}
    assert gates("read", False) == {"ro": True, "ownerOnly": False}
    assert gates("full", False) == {"ro": False, "ownerOnly": True}
    assert gates("owner", False) == {"ro": False, "ownerOnly": False}
    assert gates("read", True) == {"ro": True, "ownerOnly": False}
    assert gates("owner", True) == {"ro": True, "ownerOnly": False}
    assert gates(None, True) == {"ro": True, "ownerOnly": False}


def test_the_read_only_reasons_are_drawn() -> None:
    """Both reasons exist as the disabled controls' words: the read-only
    note at a read level, the owner-only note at a full level (a note the
    button carries - never hidden silently)."""
    assert RO_NOTE in KNOWLEDGE_SRC, "the read-only reason is missing"
    assert OWNER_NOTE in KNOWLEDGE_SRC, "the owner-only reason is missing"


def test_the_collection_writes_are_drawn_but_disabled() -> None:
    """The footer's document writes stay drawn at read level, each disabled
    with the read-only reason; the delete also at full level, with the
    owner-only reason (never hidden silently)."""
    footer = _after(KNOWLEDGE_SRC, 'data-testid="collection-share"', 1400)
    for tid in ("kn-import-zip", "kn-new-document", "kn-delete-collection"):
        w = _after(KNOWLEDGE_SRC, f'data-testid="{tid}"', 320)
        assert "disabled" in w, f"{tid} is not disabled by the level"
        assert "KN_RO_NOTE" in w or RO_NOTE in w, f"{tid} carries no read-only reason"
    assert "KN_OWNER_NOTE" in _after(KNOWLEDGE_SRC, 'data-testid="kn-delete-collection"', 320), (
        "the delete does not carry the owner-only reason at a full level"
    )
    assert "shareOpen" in footer or "setShareOpen" in footer, (
        "the Share button is not in the footer the writes share"
    )


def test_the_search_toggle_is_owner_gated() -> None:
    """Enable/disable search: drawn at every level, disabled at read (the
    read-only reason) and at full (the owner-only reason)."""
    for tid in ("kn-search-enable", "kn-search-disable"):
        w = _after(KNOWLEDGE_SRC, f'data-testid="{tid}"', 320)
        assert "disabled" in w, f"{tid} is not gated by the level"
        assert "KN_RO_NOTE" in w or RO_NOTE in w, f"{tid} carries no read-only reason"
        assert "KN_OWNER_NOTE" in w or OWNER_NOTE in w, f"{tid} carries no owner-only reason"


def test_the_document_writes_are_drawn_but_disabled() -> None:
    """New child, move, delete and save: drawn at read level, disabled with
    the read-only reason (the editor's own fields stay disabled too - a
    read grant is not an edit)."""
    for tid in ("kn-doc-new-child", "kn-doc-move", "kn-doc-delete", "kn-doc-save"):
        w = _after(KNOWLEDGE_SRC, f'data-testid="{tid}"', 320)
        assert "disabled" in w, f"{tid} is not disabled at read level"
    assert RO_NOTE in KNOWLEDGE_SRC or "KN_RO_NOTE" in KNOWLEDGE_SRC


# ===========================================================================
# The 409 answer (api.js)
# ===========================================================================


def test_harness_managed_reads_as_a_human_sentence() -> None:
    """readRefusal against the REAL ui/foundation/api.js (loaded raw, as
    ui/index.html loads it first): with the server's own words it says
    them; bare (code only) it falls back to the harness_managed sentence,
    not the code or the HTTP title. A regression in _BARE_SENTENCES fails
    here, not in a stand-in copy."""
    ctx = _mini_ctx(API_PATH.read_text(encoding="utf-8"))

    def refusal(spec: str) -> dict:
        return json.loads(
            ctx.eval("JSON.stringify(window.primerApi.readRefusal(" + spec + "))")
        )

    with_words = refusal(
        '{status: 409, detail: "this row is managed by harness h-1",'
        " envelope: {extensions: {code: 'harness_managed'}}}"
    )
    assert with_words["code"] == "harness_managed"
    assert with_words["message"] == "this row is managed by harness h-1"

    bare = refusal("{status: 409, envelope: {extensions: {code: 'harness_managed'}}}")
    assert bare["message"] == "This is shared through its harness; share the harness instead."
