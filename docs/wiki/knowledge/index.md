---
title: "Knowledge"
summary: "Collections and documents: store, find, read and search reference material, and read the platform wiki."
order: 60
---

Collections hold documents you can navigate, write and search. This wiki is itself a read-only collection. Start with [Navigate and read documents](knowledge/navigation) to read, [Search a collection](knowledge/search) to find, and [The platform wiki](knowledge/the-platform-wiki) for how this wiki is built.

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`collections_api__delete_collection_document`](reference/tools/collections_api/delete_collection_document) | Delete a document by path. | user |
| [`collections_api__find`](reference/tools/collections_api/find) | Find Collection with predicate. | user |
| [`collections_api__get_access`](reference/tools/collections_api/get_access) | Who Collection is shared with. | user |
| [`collections_api__grep_docs`](reference/tools/collections_api/grep_docs) | Regex line search across the collection's document bodies. | user |
| [`collections_api__list_indexed_documents`](reference/tools/collections_api/list_indexed_documents) | List entries indexed in a collection's vector store. | user |
| [`collections_api__put_access`](reference/tools/collections_api/put_access) | Change who Collection is shared with (owners only). | user |
| [`documents__find`](reference/tools/documents/find) | Find Document with predicate. | user |
| [`documents__list`](reference/tools/documents/list) | List Document. | user |
| [`documents__update`](reference/tools/documents/update) | Replace Document. | user |
