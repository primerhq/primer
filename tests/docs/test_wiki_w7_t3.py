"""T3 runbooks pin the live generated schemas, shipped gates and body contracts."""

import base64
import io
import json
import re
import zipfile

import jsonschema
import pytest

from primer.api.routers.knowledge import _SearchEnableBody
from primer.model.collection import Collection
from tests.docs._wiki import repo_wiki, tool_entries

SEARCH = (
    "system__get_collection", "system__list_embedding_providers", "system__list_semantic_search_providers", "system__describe_tool",
    "collections_api__enable_collection_search", "collections_api__get_collection_search_status",
    "collections_api__disable_collection_search", "collections__search",
)
INGEST = (
    "system__create_collection", "system__get_collection", "system__list_collections", "system__create_document",
    "system__put_document", "system__get_document_content", "system__list_documents", "system__describe_tool",
    "collections_api__import_collection_zip", "collections__collection_tree", "collections__search",
)
FLOWS = {"runbooks/enable-collection-search": SEARCH, "runbooks/ingest-a-knowledge-base": INGEST}
GATED = {
    "collections_api__enable_collection_search", "collections_api__disable_collection_search", "collections_api__import_collection_zip",
}


def calls(path):
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", repo_wiki().pages[path].body, re.S)]


@pytest.mark.parametrize("path", FLOWS)
def test_t3_runbooks_follow_conventions_and_link_every_exact_tool(path):
    pages = repo_wiki().pages
    page = pages[path]
    assert page.kind == "guide" and page.meta["audience"] == "admin"
    assert page.meta["tools"] == list(FLOWS[path])
    assert re.findall(r"^## (.+)$", page.body, re.M) == [
        "Goal", "Preconditions", "Roles and gates", "Steps", "Verification", "Refusal and failure", "Related",
    ]
    for scoped in FLOWS[path]:
        reference = "reference/tools/" + scoped.replace("__", "/")
        assert reference in pages and f"]({reference})" in page.body
    assert pages["purpose-map"].body.count(f"]({path})") == 2
    assert f"]({path})" in pages["runbooks"].body


@pytest.mark.parametrize("path", FLOWS)
def test_all_worked_meta_calls_and_inner_arguments_match_current_schemas(path):
    entries = {e.scoped_id: e for e in tool_entries()}
    seen = set()
    described = set()
    for call in calls(path):
        jsonschema.validate(call, entries["system__call_tool"].tool.args_schema)
        scoped = call["toolset_id"] + "__" + call["tool_name"]
        assert scoped in FLOWS[path]
        jsonschema.validate(call["arguments"], entries[scoped].tool.args_schema)
        seen.add(scoped)
        if scoped == "system__describe_tool":
            described.add(call["arguments"]["toolset_id"] + "__" + call["arguments"]["tool_name"])
    assert GATED.intersection(FLOWS[path]) <= described
    # Metadata-only create is explicitly warned about, not presented as an
    # ingestion write. The content put/read pair must actually be sampled.
    assert set(FLOWS[path]) - {"system__create_document"} <= seen


@pytest.mark.parametrize("scoped", sorted(set(SEARCH + INGEST)))
def test_role_floor_and_shipped_gate_are_pinned_per_exact_tool(scoped):
    entry = next(e for e in tool_entries() if e.scoped_id == scoped)
    assert entry.required_role == ("admin" if scoped in {
        "system__list_embedding_providers", "system__list_semantic_search_providers",
    } else "user")
    assert entry.default_gate == ("required" if scoped in GATED else None)
    if scoped.startswith("collections_api__"):
        assert entry.tool.read_only is (scoped not in GATED)


def test_search_body_has_no_client_lifecycle_state_and_is_not_a_provider_create():
    worked = next(c["arguments"]["body"] for c in calls("runbooks/enable-collection-search") if c["tool_name"] == "enable_collection_search")
    body = _SearchEnableBody.model_validate(worked)
    assert body.embedder.provider_id == "team-embedder" and body.embedder.model == "team-embedding"
    assert body.vector_store_provider_id == "team-ssp" and body.cross_encoder is None
    assert "state" not in worked and "error" not in worked
    assert Collection(id="sample", description="sample knowledge").search is None


def test_zip_example_decodes_to_actual_utf8_tree_content_and_uses_generated_multipart_shape():
    worked = next(c["arguments"] for c in calls("runbooks/ingest-a-knowledge-base") if c["tool_name"] == "import_collection_zip")
    raw = base64.b64decode(worked["body"]["file"], validate=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert z.namelist() == ["Guides/Oncall.md"]
        assert z.read("Guides/Oncall.md").decode() == "# Cedar on-call\nCedar escalation uses the blue pager.\n"
    assert worked["parent"] == "" and worked["conflict"] == "fail"
    entry = next(e for e in tool_entries() if e.scoped_id == "collections_api__import_collection_zip")
    body = entry.tool.args_schema["$defs"][entry.tool.args_schema["properties"]["body"]["$ref"].split("/")[-1]]
    assert body["properties"]["file"]["contentEncoding"] == "base64"


def test_runbooks_distinguish_owner_full_partial_import_and_semantic_proof():
    search = repo_wiki().pages["runbooks/enable-collection-search"].body
    ingest = repo_wiki().pages["runbooks/ingest-a-knowledge-base"].body
    assert "Full access alone does not permit" in search
    assert "A non-owner with full may import" in ingest
    assert "state=error" in search and "HTTP 200" in search
    assert "inline backfill" in search and "do not retry" in search
    assert "extensions.report" in ingest and "earlier writes can remain" in ingest
    assert "metadata alone" in ingest and "content store" in ingest
    assert "mode_used=semantic" in search and "mode_used=semantic" in ingest
    assert any(c["arguments"].get("mode") == "semantic" for c in calls("runbooks/enable-collection-search"))
    assert any(c["arguments"].get("mode") == "semantic" for c in calls("runbooks/ingest-a-knowledge-base"))
