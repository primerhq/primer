"""The four toolsets feature pages (pivot P4, task 12).

The built-in toolsets page names every reserved toolset id (its drift guard
lives in ``tests.docs._wiki_specs.DRIFT_GUARDS``), the MCP and Python pages
document the stored toolsets and their tools, and the list-and-describe page
is the home of ``call_tool``, ``describe_tool`` and ``list_toolset_tools``.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("toolsets/builtin-toolsets", "Built-in toolsets",
             "The toolsets that ship with primer, what each one is for and which ones need a workspace session.",
             "feature", 1,
             related=("toolsets/list-and-describe-tools", "start-here/calling-tools")),
    PageSpec("toolsets/mcp-toolsets", "MCP toolsets",
             "Register an MCP server as a toolset, change it, and read its sign-in status.",
             "feature", 2,
             tools=scoped("system", "list_toolsets get_toolset create_toolset update_toolset delete_toolset find_toolsets"),
             tools_if_present=scoped("system", "get_toolset_auth_status"),
             related=("toolsets/list-and-describe-tools", "approvals/policies")),
    PageSpec("toolsets/python-toolsets", "Python toolsets",
             "Turn a Python module into a toolset: how functions become tools, and how to create and update one.",
             "feature", 3,
             tools=scoped("crud", "create_python_toolset update_python_toolset_source list_python_tools"),
             related=("toolsets/mcp-toolsets", "approvals/policies")),
    PageSpec("toolsets/list-and-describe-tools", "List and describe tools",
             "Find a tool, read its schema and call it through call_tool.",
             "feature", 4,
             tools=scoped("system", "list_toolset_tools describe_tool call_tool"),
             related=("start-here/calling-tools", "toolsets/builtin-toolsets", "knowledge/the-platform-wiki"),
             must=("forbidden", "describe_tool")),
)
