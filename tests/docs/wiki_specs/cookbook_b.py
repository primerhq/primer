"""The scheduled-work cookbook recipes (pivot P4, task 19).

One :class:`PageSpec` per recipe: the planned path, the exact frontmatter
(title, summary, order), the scoped tools the recipe calls and the related
paths. A recipe calls exactly the tools it lists, in a sound order
(:data:`RECIPE_ORDER`), and every JSON example validates against the live
schemas; ``tests/docs/test_wiki_pages.py`` checks all of it.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("cookbook/scheduled-summariser", "Scheduled summariser",
             "Run an agent every weekday morning to summarise yesterday's logs and post the result to Slack.",
             "recipe", 5,
             tools=scoped("system", "create_agent") + scoped("workspaces", "create_workspace_template create_workspace") + scoped("trigger", "create create_subscription fire_now") + scoped("workspaces", "list_workspace_sessions get_workspace_session"),
             related=("triggers/triggers", "channels/reply-binding"),
             max_chars=10000),
    PageSpec("cookbook/daily-incident-digest", "Daily incident digest",
             "Every weekday, an agent reads overnight incidents from a collection and posts a digest to a Slack channel.",
             "recipe", 6,
             tools=scoped("system", "create_collection create_agent create_channel") + scoped("trigger", "create create_subscription fire_now") + scoped("workspaces", "list_workspace_sessions get_workspace_session"),
             related=("triggers/subscriptions", "knowledge/collections"),
             max_chars=10000),
    PageSpec("cookbook/pr-reviewer-on-cron", "Pull request reviewer on a schedule",
             "An hourly scheduled agent reviews new pull requests in a git workspace and comments back.",
             "recipe", 7,
             tools=scoped("system", "create_agent") + scoped("workspaces", "create_workspace_template create_workspace") + scoped("trigger", "create create_subscription fire_now") + scoped("workspaces", "list_workspace_sessions get_workspace_session"),
             related=("triggers/triggers", "workspaces/templates"),
             max_chars=10000),
    PageSpec("cookbook/event-driven-data-pipeline", "Event-driven data pipeline",
             "A scheduled trigger starts an agent that normalises a payload and ingests it into a collection.",
             "recipe", 8,
             tools=scoped("system", "create_collection create_agent") + scoped("workspaces", "create_workspace") + scoped("trigger", "create create_subscription fire_now") + scoped("workspaces", "get_workspace_session") + scoped("system", "list_documents"),
             related=("triggers/subscriptions", "knowledge/documents"),
             max_chars=10000),
)
