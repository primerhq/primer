"""The channels cookbook recipes (pivot P4, task 20).

One :class:`PageSpec` per recipe: the planned path, the exact frontmatter
(title, summary, order), the scoped tools the recipe calls and the related
paths. A recipe calls exactly the tools it lists, in a sound order
(:data:`RECIPE_ORDER`), and every JSON example validates against the live
schemas; ``tests/docs/test_wiki_pages.py`` checks all of it.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("cookbook/slack-question-answerer", "Slack question answerer",
             "Mentioning a bot in Slack runs an agent that answers from a knowledge collection.",
             "recipe", 9,
             audience="admin",
             tools=scoped("system", "create_channel_provider create_channel") + scoped("workspaces", "create_workspace") + scoped("system", "create_agent") + scoped("trigger", "create") + scoped("system", "create_channel_binding") + scoped("workspaces", "list_workspace_sessions get_workspace_session"),
             related=("channels/bindings", "channels/providers"),
             max_chars=10000),
    PageSpec("cookbook/telegram-personal-assistant", "Telegram personal assistant",
             "Direct messages to a Telegram bot continue one durable session across messages.",
             "recipe", 10,
             audience="admin",
             tools=scoped("system", "create_channel_provider create_channel") + scoped("workspaces", "create_workspace") + scoped("system", "create_agent") + scoped("trigger", "create") + scoped("system", "create_channel_binding") + scoped("workspaces", "list_workspace_sessions get_workspace_session"),
             related=("channels/bindings", "channels/providers"),
             max_chars=10000),
    PageSpec("cookbook/discord-moderation-helper", "Discord moderation helper",
             "Route Discord messages to a classifier agent and gate its delete tool behind a person's approval.",
             "recipe", 11,
             audience="admin",
             tools=scoped("system", "create_channel_provider create_channel") + scoped("workspaces", "create_workspace") + scoped("system", "create_agent") + scoped("trigger", "create") + scoped("system", "create_channel_binding create_tool_approval_policy") + scoped("workspaces", "list_workspace_sessions get_workspace_session"),
             related=("channels/bindings", "approvals/policies"),
             max_chars=10000),
    PageSpec("cookbook/approval-gated-deploy-bot", "Approval-gated deploy bot",
             "A deploy agent whose deploy tool is gated: the turn pauses until a person approves in the console or in Slack.",
             "recipe", 12,
             audience="admin",
             tools=scoped("system", "create_channel_provider create_channel") + scoped("workspaces", "create_workspace") + scoped("system", "create_agent set_reply_binding create_tool_approval_policy") + scoped("workspaces", "create_workspace_session get_workspace_session"),
             related=("approvals/policies", "channels/reply-binding"),
             max_chars=10000),
)
