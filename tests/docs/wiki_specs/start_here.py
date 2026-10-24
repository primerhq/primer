"""The start-here index and its five pages (pivot P4, task 2).

P1 wrote the pages (pivot P1, task 14); this task pins them: the exact
frontmatter the build must hold, the tools each page documents and shows in a
worked call, the related paths and the strings the body must say. The pages
keep P1's facts and gain the section map, the decision order, the rank
sentence, the two new peer-message error types and the core-kit worked calls.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("start-here", "Start here",
             "Read these first: how to read this wiki and how to call tools.",
             "section", 10),
    PageSpec("start-here/reading-this-wiki", "Reading this wiki",
             "How the wiki is laid out, how to read and search it, and how to find the right page for a task.",
             "feature", 1,
             tools=scoped("system", "wiki_read wiki_search describe_tool"),
             related=("start-here/calling-tools", "knowledge/the-platform-wiki"),
             must=("this-install", "reference/tools")),
    PageSpec("start-here/calling-tools", "Calling tools",
             "The call_tool contract: scoped ids, arguments, results, pausing tools, and the tools you call directly.",
             "feature", 2,
             tools=scoped("system", "call_tool describe_tool list_toolset_tools"),
             related=("start-here/roles-and-approvals", "start-here/errors", "toolsets/list-and-describe-tools"),
             must=("describe_tool", "forbidden")),
    PageSpec("start-here/roles-and-approvals", "Roles and approvals",
             "Who may call what: roles, the run's principal, forbidden errors and approval gates.",
             "feature", 3,
             related=("start-here/errors", "approvals/policies", "agents/tools-and-grants"),
             must=("rank",)),
    PageSpec("start-here/errors", "Errors",
             "The error envelope and every error type, with what to do next.",
             "feature", 4,
             related=("start-here/calling-tools", "start-here/roles-and-approvals"),
             must=("hop-limit", "rate-limited", "unavailable")),
    PageSpec("start-here/the-core-kit", "The core kit",
             "The five tools every seeded agent carries and why everything else goes through call_tool.",
             "feature", 5,
             tools=scoped("system", "call_tool wiki_read wiki_search ask_user") + scoped("misc", "inform_user"),
             related=("start-here/calling-tools", "agents/tools-and-grants", "human-in-the-loop/ask-user"),
             must=("list_my_sessions", "message_session")),
)
