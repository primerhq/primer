"""The exact M4 docs contract of the internal-collections surface.

The runbook page is not a spec page (its frontmatter follows the runbook conventions - no ``order``), so the
parametrised page tests of ``tests/docs/test_wiki_pages.py`` do not cover it: this module pins what the M4 docs
MUST say, from the pages' own frontmatter and bodies - the runbook's exact tool list and related paths, its whole
IC surface against the live catalogue, the exact reference citations and worked calls (the nested config-write
body, the zero-argument writes, the behavior search), the no-silent-default option set of step 1, the gate table
(the three writes ship the ``required`` admin-only gate, the two zero-argument writes with their bounded cards),
the two reads shipping ungated, the roles claim limited to the IC surface, the purpose-map and runbook-index rows,
and the not-agent-callable entry's retirement (the subsystem now has agent tools).
"""

from __future__ import annotations

from primer.knowledge.wiki.pages import parse_page_source
from tests.docs._wiki import WIKI_DIR

PAGE = WIKI_DIR / "runbooks" / "configure-internal-collections.md"

# The flow's tools, in the page's order: the five generated internal-collections tools (the M4 surface), the
# verification search, and the provider-config tools the preconditions and step 1 use.
_TOOLS = [
    "internal_collections__get_config",
    "internal_collections__put_config",
    "internal_collections__delete_config",
    "internal_collections__bootstrap",
    "internal_collections__bootstrap_status",
    "collections__search",
    "system__get_embedding_provider",
    "system__check_huggingface_models",
    "system__test_embedding_provider",
    "system__create_embedding_provider",
    "system__get_semantic_search_provider",
    "system__ask_user",
]
_RELATED = [
    "purpose-map",
    "providers/embedding",
    "providers/embedding-model-options",
    "providers/semantic-search",
    "runbooks/conventions",
]


def _page():
    assert PAGE.is_file(), f"{PAGE} is missing - the M4 runbook has no home"
    return parse_page_source(PAGE.read_text(encoding="utf-8"), path="runbooks/configure-internal-collections")


def test_the_runbook_parses_with_exactly_the_m4_frontmatter() -> None:
    page = _page()
    assert page.title == "Enable the internal collections subsystem"
    assert page.summary == (
        "Turn on semantic search over the platform's own document corpus: choose or verify the "
        "embedding provider, write the config, bootstrap, verify."
    )
    assert page.meta.get("audience") == "admin"
    assert page.meta.get("tools", []) == _TOOLS
    assert page.meta.get("related", []) == _RELATED


def test_the_gate_table_names_the_three_writes_required_and_admin_only() -> None:
    """The M4 gate surface, exactly: the three writes ship the required gate, decided by an admin only; the
    zero-argument writes carry the intentional bounded card."""
    body = _page().body
    for line in (
        "| `internal_collections__put_config` | `required` | an admin | the vector-space choice: the embedding provider + model, the search provider, the reranker when named |",
        "| `internal_collections__delete_config` | `required` | an admin | (no arguments) the deactivation itself: an intentional bounded card that names the call and shows no value |",
        "| `internal_collections__bootstrap` | `required` | an admin | (no arguments) the activation itself: an intentional bounded card that names the call and shows no value |",
    ):
        assert line in body, f"the gate table must hold this exact row:\n{line}"


def test_the_two_reads_ship_ungated() -> None:
    assert (
        "The two reads (`internal_collections__get_config`, `internal_collections__bootstrap_status`) ship ungated."
        in _page().body
    )


def test_the_roles_claim_is_limited_to_the_ic_surface() -> None:
    """The admin-tier claim is limited to the IC surface: of the flow's twelve tools, the five
    ``internal_collections`` tools and the five ``system__`` provider-config helpers are admin-tier, and only
    ``system__ask_user`` and ``collections__search`` carry the user role (claiming every flow tool is admin
    would send a user-role run to a call it cannot make)."""
    body = _page().body
    assert "the five `internal_collections` tools carry `required_role=\"admin\"`" in body
    assert "only the two interactions, `system__ask_user` and `collections__search`, carry the user role" in body
    assert "every step's tool carries" not in body, "the overbroad roles claim is back"


# ---- the IC surface against the live catalogue and the runbook's calls ----------------------------------------------


def test_the_runbook_documents_the_whole_ic_surface() -> None:
    """The runbook's ``internal_collections`` ids are exactly the IC tools the live catalogue ships (a drift guard:
    a sixth IC tool lands in the code and fails the runbook's tool list until it documents it)."""
    from tests.docs._wiki import tool_entries

    live_ic = {entry.scoped_id for entry in tool_entries() if entry.toolset_id == "internal_collections"}
    assert {tool for tool in _TOOLS if tool.startswith("internal_collections__")} == live_ic


def test_the_frontmatter_tool_ids_are_all_live() -> None:
    from tests.docs._wiki import tool_entries

    live = {entry.scoped_id for entry in tool_entries()}
    missing = [tool for tool in _TOOLS if tool not in live]
    assert missing == [], f"the runbook names tools the live catalogue has not shipped: {missing}"


def test_the_five_ic_tools_are_cited_with_their_own_reference_links() -> None:
    """The citation rule, exactly for the M4 surface: every IC tool is backticked by its scoped id and linked to
    its own reference page, and no other IC tool is cited."""
    import re

    body = _page().body
    cited = set(re.findall(r"`internal_collections__([a-z0-9_]+)`", body))
    assert cited == {"get_config", "put_config", "delete_config", "bootstrap", "bootstrap_status"}, cited
    for name in sorted(cited):
        assert f"](reference/tools/internal_collections/{name})" in body, f"{name}: no reference-page link"


def test_the_runbook_shows_exactly_the_three_worked_calls() -> None:
    """The runbook's json blocks call exactly three tools, in the flow's order: the config write, the bootstrap,
    the behavior search (a fourth call, or a reordered flow, is a red suite)."""
    from tests.docs._wiki_specs import calls_in_body

    assert calls_in_body(_page().body) == [
        "internal_collections__put_config",
        "internal_collections__bootstrap",
        "collections__search",
    ]


def test_the_put_config_call_carries_the_nested_body() -> None:
    """The config write nests its arguments under ``body`` (the generated tool's schema), with the seeded
    provider + model and the reserved search provider."""
    import json

    body = _page().body
    line = next((ln for ln in body.splitlines() if '"tool_name": "put_config"' in ln), None)
    assert line is not None, "the runbook must show the config write's call body"
    assert json.loads(line) == {
        "toolset_id": "internal_collections",
        "tool_name": "put_config",
        "arguments": {
            "body": {
                "embedding_provider_id": "huggingface",
                "embedding_model": "BAAI/bge-small-en-v1.5",
                "search_provider_id": "lance",
            }
        },
    }


def test_the_bootstrap_and_search_calls_are_the_exact_zero_and_behavior_calls() -> None:
    body = _page().body
    assert '{"toolset_id": "internal_collections", "tool_name": "bootstrap", "arguments": {}}' in body
    assert (
        '{"toolset_id": "collections", "tool_name": "search", '
        '"arguments": {"collection": "system", "query": "enable the internal collections subsystem", "limit": 5}}'
    ) in body


def test_step_1_presents_the_option_set_with_no_silent_default() -> None:
    """OQ-2, in the runbook: step 1 presents the three-tier option set and the pick is confirmed, never silent."""
    body = _page().body
    assert "### 1. Choose the embedding model (no silent default)" in body
    assert "three tiers spanning memory cost (small, mid, large)" in body
    assert "the pick is never silent (OQ-2)" in body


# ---- the index rows and the retired entry --------------------------------------------------------------------------


def test_the_index_rows_point_at_the_runbook() -> None:
    purpose = (WIKI_DIR / "purpose-map.md").read_text(encoding="utf-8")
    assert (
        "| enable the internal collections subsystem (the platform's own wiki store: embedding provider, config, bootstrap) "
        "| [runbooks/configure-internal-collections](runbooks/configure-internal-collections) |"
    ) in purpose
    assert "pending: runbooks/configure-internal-collections" not in purpose, "the pending marker is back"

    index = (WIKI_DIR / "runbooks" / "index.md").read_text(encoding="utf-8")
    assert (
        "| [runbooks/configure-internal-collections](runbooks/configure-internal-collections) "
        "| enable the internal collections subsystem (the platform's own wiki store: model choice, config write, bootstrap, verify) |"
    ) in index


def test_the_not_agent_callable_index_no_longer_lists_the_subsystem() -> None:
    """M4 retires the not-agent-callable entry: the subsystem has agent tools (the five generated IC tools), so the
    entry that sent its flow to the raw REST routes is gone."""
    text = (WIKI_DIR / "not-agent-callable" / "index.md").read_text(encoding="utf-8")
    assert "internal-collections" not in text and "internal collections subsystem" not in text
