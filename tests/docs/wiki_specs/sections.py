"""The sixteen section index pages (pivot P4, task 1).

One :class:`PageSpec` per section: the planned path, the exact frontmatter
(title, summary, audience, order) and the ``section`` kind. The section
template rule (the intro is at least 80 characters; the build adds the
``## Pages`` list) is checked by ``tests/docs/test_wiki_pages.py``. A feature
or recipe spec added by a later content task carries its ``tools``,
``tools_if_present``, ``related``, ``must`` and ``max_chars``.
"""

from tests.docs._wiki_specs import PageSpec

SPECS = (
    PageSpec("agents", "Agents",
             "Define, edit, run and hand off agents: prompts, tool grants, model profiles and compaction.",
             "section", 20),
    PageSpec("sessions", "Sessions",
             "Start, watch, control and message sessions: the runs of agents and graphs inside a workspace.",
             "section", 30),
    PageSpec("workspaces", "Workspaces",
             "Create and manage workspaces: files, templates, providers, agents.md and the prime workspace.",
             "section", 40),
    PageSpec("graphs", "Graphs",
             "Build and run graphs: multi-step workflows of agents, tools and sub-graphs.",
             "section", 50),
    PageSpec("knowledge", "Knowledge",
             "Collections and documents: store, find, read and search reference material, and read the platform wiki.",
             "section", 60),
    PageSpec("triggers", "Triggers",
             "Start work from schedules, webhooks and channel events, and pause a turn until an event happens.",
             "section", 70),
    PageSpec("channels", "Channels",
             "Connect Slack, Telegram and Discord: providers, channels, inbound bindings and reply bindings.",
             "section", 80),
    PageSpec("human-in-the-loop", "Human in the loop",
             "Ask a person, tell a person, show a file and draw on the canvas: every way an agent reaches the user.",
             "section", 90),
    PageSpec("toolsets", "Toolsets",
             "The built-in, MCP and Python toolsets, and how to list, describe and call their tools.",
             "section", 100),
    PageSpec("providers", "Providers",
             "Admin: register the LLM, embedding, cross-encoder, vector-store, storage and web providers that agents run on.",
             "section", 110,
             audience="admin"),
    PageSpec("approvals", "Approvals",
             "Admin: gate tool calls behind a person, a rule or a judge model with approval policies.",
             "section", 120,
             audience="admin"),
    PageSpec("harnesses", "Harnesses",
             "Install bundles of agents, graphs and documents from a git repository.",
             "section", 130),
    PageSpec("web", "Web",
             "Search the web, fetch pages, make HTTP requests and download files.",
             "section", 140),
    PageSpec("utilities", "Utilities",
             "Small helpers: time, ids, hashes, math, sleeping, watching files and reading office documents.",
             "section", 150),
    PageSpec("not-agent-callable", "Not agent-callable yet",
             "Features only the console and REST can drive today, and what an agent can do instead.",
             "section", 160),
    PageSpec("cookbook", "Cookbook",
             "End-to-end recipes in call_tool form, from running one session to running a task through prime.",
             "section", 170),
)
