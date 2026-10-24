"""The providers pages for the infrastructure families (pivot P4, task 14).

Three admin feature pages. ``semantic-search`` is the vector-store family
(the ``invalidate_`` tool is a live tool, not a conditional one).
``artifact-storage`` lists the probe and the active-store pair as
conditional: they are shown when live. ``web-search-and-fetch`` has no
unconditional tools: the four active-config tools are conditional, and the
page documents them when they are live.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("providers/semantic-search", "Semantic-search providers",
             "Admin: register the vector stores that hold collection embeddings.",
             "feature", 5,
             audience="admin",
             tools=scoped("system", "list_semantic_search_providers get_semantic_search_provider create_semantic_search_provider update_semantic_search_provider delete_semantic_search_provider find_semantic_search_providers invalidate_semantic_search_provider"),
             related=("providers/embedding", "knowledge/search")),
    PageSpec("providers/artifact-storage", "Artifact storage",
             "Admin: choose where files and large outputs are stored, and probe a store before using it.",
             "feature", 6,
             audience="admin",
             tools=scoped("system", "list_artifact_storage_providers get_artifact_storage_provider create_artifact_storage_provider update_artifact_storage_provider delete_artifact_storage_provider find_artifact_storage_providers"),
             tools_if_present=scoped("system", "test_artifact_storage_provider get_active_artifact_store set_active_artifact_store"),
             related=("workspaces/what-a-workspace-is",)),
    PageSpec("providers/web-search-and-fetch", "Web search and fetch settings",
             "Admin: which provider answers web searches and which fetcher reads pages. Agents use the web tools.",
             "feature", 7,
             audience="admin",
             tools_if_present=scoped("system", "get_web_search_active_config set_web_search_active_config get_web_fetch_active_config set_web_fetch_active_config"),
             related=("web/search-and-fetch", "not-agent-callable")),
)
