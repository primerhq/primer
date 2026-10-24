"""The five knowledge feature pages (pivot P4, task 7).

The collections, documents, search and navigation pages document the
``collections`` and ``system`` families of collection and document tools,
and ``the-platform-wiki`` documents the wiki's own system collection and
its two tools.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("knowledge/collections", "Collections",
             "A collection is a named tree of documents with optional search: create, change, find, refresh and delete collections.",
             "feature", 1,
             tools=scoped("system", "list_collections get_collection create_collection update_collection delete_collection find_collections refresh_collection"),
             related=("knowledge/documents", "knowledge/search", "providers/semantic-search")),
    PageSpec("knowledge/documents", "Write documents",
             "Create, replace, move and delete documents by path, with the collections tools or the system document tools.",
             "feature", 2,
             tools=scoped("collections", "create_document update_document move_document delete_document") + scoped("system", "put_document create_document update_document delete_document move_document"),
             related=("knowledge/navigation", "knowledge/collections"),
             max_chars=12000),
    PageSpec("knowledge/search", "Search a collection",
             "Search a collection by meaning, keywords, exact text or pattern, and what each mode needs.",
             "feature", 3,
             tools=scoped("collections", "search") + scoped("system", "search_collection"),
             related=("knowledge/navigation", "providers/semantic-search", "knowledge/the-platform-wiki")),
    PageSpec("knowledge/navigation", "Navigate and read documents",
             "List collections, walk a collection's tree, read a document and filter by metadata.",
             "feature", 4,
             tools=scoped("collections", "collections_list collection_tree read_document") + scoped("system", "list_documents get_document get_document_content list_collection_documents find_documents find_collection_documents_by_meta"),
             related=("knowledge/search", "knowledge/documents"),
             max_chars=12000),
    PageSpec("knowledge/the-platform-wiki", "The platform wiki",
             "This wiki is the read-only system collection: how it is built, how to read and search it, and where tool reference pages live.",
             "feature", 5,
             tools=scoped("system", "wiki_read wiki_search"),
             related=("start-here/reading-this-wiki", "knowledge/search", "toolsets/list-and-describe-tools"),
             must=("this-install", "reference/tools")),
)
