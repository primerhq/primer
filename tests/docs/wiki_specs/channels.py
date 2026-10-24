"""The channels pages (pivot P4, task 9).

One :class:`PageSpec` per page of the section: channel providers (admin: the
Slack, Telegram or Discord integration), channels (one room on a provider),
inbound bindings (a channel trigger plus a binding that routes a message to
an agent, a graph or a session) and the workspace reply binding (where a
workspace's questions, approvals and updates go).
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("channels/providers", "Channel providers",
             "Admin: register the Slack, Telegram or Discord integration that channels belong to.",
             "feature", 1,
             audience="admin",
             tools=scoped("system", "list_channel_providers get_channel_provider create_channel_provider update_channel_provider delete_channel_provider find_channel_providers"),
             related=("channels/channels", "not-agent-callable")),
    PageSpec("channels/channels", "Channels",
             "A channel is one room on a provider. Create and change channels, and let a room start sessions.",
             "feature", 2,
             tools=scoped("system", "list_channels get_channel create_channel update_channel delete_channel find_channels"),
             related=("channels/providers", "channels/bindings", "channels/reply-binding")),
    PageSpec("channels/bindings", "Inbound bindings",
             "Route channel messages to agents: a channel trigger plus a binding with a matcher, an action and a reply target.",
             "feature", 3,
             tools=scoped("system", "create_channel_binding list_channel_bindings delete_channel_binding"),
             related=("triggers/triggers", "triggers/subscriptions", "channels/reply-binding"),
             must=("event_matcher", "reply_target"),
             max_chars=12000),
    PageSpec("channels/reply-binding", "Reply binding",
             "Send a workspace's questions, approvals and updates to a channel by binding the workspace to it.",
             "feature", 4,
             tools=scoped("system", "set_reply_binding clear_reply_binding"),
             related=("channels/channels", "human-in-the-loop/ask-user", "approvals/policies"),
             must=("reply_binding",)),
)
