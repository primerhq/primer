---
title: "Toolsets"
summary: "The built-in, MCP and Python toolsets, and how to list, describe and call their tools."
order: 100
---

A toolset is a named group of tools. These pages list the built-in toolsets, show how to register MCP and Python toolsets, and explain how to list, describe and call any tool. Start with [List and describe tools](toolsets/list-and-describe-tools).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`toolsets__find`](reference/tools/toolsets/find) | Find Toolset with predicate. | user |
| [`toolsets__get_access`](reference/tools/toolsets/get_access) | Who Toolset is shared with. | user |
| [`toolsets__get_toolset_with_builtin_shim`](reference/tools/toolsets/get_toolset_with_builtin_shim) | Get a toolset (built-in synthesised, user-defined from storage). | user |
| [`toolsets__invalidate_toolset_provider`](reference/tools/toolsets/invalidate_toolset_provider) | Invalidate cached toolset provider. | user |
| [`toolsets__list_builtin_toolsets`](reference/tools/toolsets/list_builtin_toolsets) | List the built-in toolsets an agent can bind. | user |
| [`toolsets__put_access`](reference/tools/toolsets/put_access) | Change who Toolset is shared with (owners only). | user |
| [`toolsets__toolset_runtime`](reference/tools/toolsets/toolset_runtime) | Runtime facts about a toolset: isolation level and derived tools. | user |
| [`toolsets__validate_python_source`](reference/tools/toolsets/validate_python_source) | Dry-run: what would this source register, and what is wrong with it. | user |
