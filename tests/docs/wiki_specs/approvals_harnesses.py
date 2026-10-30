"""The approvals and harnesses pages (pivot P4, task 15).

Three feature pages. ``approvals/policies`` is the admin home of the six
approval-policy tools of the ``system`` toolset and carries the approval-types
drift guard (``tests.docs._wiki_specs.DRIFT_GUARDS``). ``harnesses/what-a-harness-is``
documents the two harness reads every user may call; ``harnesses/install-and-sync``
is the admin home of the seven harness writes. The harness tools carry the bare ids
(``list``, ``get``, ...), scoped like every other toolset to ``harness__<tool>``.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("approvals/policies", "Approval policies",
             "Admin: gate a tool behind a person, a rule or a judge model. How gates pause a turn and how a decision is made.",
             "feature", 1,
             audience="admin",
             tools=scoped("system", "list_tool_approval_policies get_tool_approval_policy create_tool_approval_policy update_tool_approval_policy delete_tool_approval_policy find_tool_approval_policies"),
             related=("start-here/roles-and-approvals", "sessions/control-a-session", "channels/reply-binding"),
             must=("approvers", "preview_args"),
             max_chars=12000),
    PageSpec("harnesses/what-a-harness-is", "What a harness is",
             "A harness is a git repository of agents, graphs and documents that primer installs and keeps in sync. List and inspect them.",
             "feature", 1,
             tools=scoped("harness", "list get"),
             related=("harnesses/install-and-sync", "agents/create-and-edit")),
    PageSpec("harnesses/install-and-sync", "Install and sync a harness",
             "Admin: register, configure, fetch, install, sync and uninstall a harness.",
             "feature", 2,
             audience="admin",
             tools=scoped("harness", "register update update_overrides fetch install sync uninstall"),
             related=("harnesses/what-a-harness-is",),
             max_chars=12000),
)
