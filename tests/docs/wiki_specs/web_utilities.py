"""The web and utilities pages (pivot P4, task 16).

Five user-tier feature pages. ``web/search-and-fetch`` and
``web/http-and-downloads`` own the ``web`` toolset's four tools; the
``providers/web-search-and-fetch`` page (task 14) owns the admin side of
the web settings. ``utilities/time-ids-hashes-math`` owns the four
``misc`` tools. ``utilities/sleep-and-watch-files`` owns the two
yielding ``workspace_ext`` tools. ``utilities/read-office-documents``
owns ``system__read_doc_content``.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("web/search-and-fetch", "Search and fetch",
             "Search the web and read a page as clean markdown.",
             "feature", 1,
             tools=scoped("web", "web_search web_fetch"),
             related=("web/http-and-downloads", "providers/web-search-and-fetch")),
    PageSpec("web/http-and-downloads", "HTTP requests and downloads",
             "Call an HTTP API and download a file into the workspace.",
             "feature", 2,
             tools=scoped("web", "http_request download"),
             related=("web/search-and-fetch", "approvals/policies"),
             must=("PRIMER_EGRESS_ALLOW",)),
    PageSpec("utilities/time-ids-hashes-math", "Time, ids, hashes and math",
             "Get the date and time, mint UUIDs, hash text and calculate safely.",
             "feature", 1,
             tools=scoped("misc", "get_datetime uuid_v4 hash calculate"),
             related=("utilities/sleep-and-watch-files",)),
    PageSpec("utilities/sleep-and-watch-files", "Sleep and watch files",
             "Pause a turn for a set time or until a workspace file changes.",
             "feature", 2,
             tools=scoped("workspace_ext", "sleep watch_files"),
             related=("triggers/waiting-for-events", "workspaces/files-and-log")),
    PageSpec("utilities/read-office-documents", "Read office documents",
             "Convert a PDF, Word, PowerPoint or HTML file in the workspace to plain text.",
             "feature", 3,
             tools=scoped("system", "read_doc_content"),
             related=("workspaces/in-session-file-tools", "knowledge/documents")),
)
