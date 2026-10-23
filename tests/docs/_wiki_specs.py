"""The page specs of the platform wiki (pivot P4, task 1).

A wiki page is written from its spec, not freehand. A :class:`PageSpec` is the
contract of one page: its planned path, the exact title and summary the
frontmatter must carry, the audience and order, the scoped tool ids the page
must document and show, the conditional tools it must show only if they are
live, the related paths, the strings the body must say and the length cap.
The specs live in the modules of ``tests/docs/wiki_specs/`` (one module per
section; task 1 adds ``sections.py`` with the sixteen section rows, and each
content task adds its own), and :func:`all_specs` merges them: a path defined
twice is an ``AssertionError``, not a race.

``render_frontmatter`` is the head printer: it writes exactly the frontmatter
a spec implies (title, summary, audience when admin, order, the expected
tools and the related paths) and every page starts from its printed head.
``tests/docs/test_wiki_pages.py`` checks every built page against its spec:
the frontmatter matches, the template headings are in order, every tool the
spec lists exists and is linked to its reference page and shown in a worked
call, a recipe calls exactly the tools it lists in a sound order
(:data:`RECIPE_ORDER`), the related paths resolve, and the drift guards
(:data:`DRIFT_GUARDS`) keep a page naming everything the code defines.

``PLANNED_PATHS`` is the closed set of the paths the wiki may hold: a page
for a path outside it is a bug, and while ``STRICT`` is False a spec whose
page is not written yet skips its page tests (task 23 sets it True and drops
the ``PLANNED_PATHS`` allowance from the link tests).
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from primer.knowledge.wiki.pages import SUMMARY_MAX
from tests.docs._wiki import tool_entries

EM_DASH = chr(0x2014)
_PAGE_KINDS = ("section", "feature", "recipe")
_AUDIENCES = ("user", "admin")
_MAX_CHARS_CEIL = 14000


@dataclass(frozen=True)
class PageSpec:
    """One page's contract: the planned path, the exact frontmatter, the tools and related paths, the must-strings, the cap."""

    path: str
    title: str
    summary: str
    kind: Literal["section", "feature", "recipe"]
    order: int
    audience: Literal["user", "admin"] = "user"
    tools: tuple[str, ...] = ()
    tools_if_present: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    must: tuple[str, ...] = ()
    max_chars: int = 9000

    def __post_init__(self) -> None:
        for label, text in (("title", self.title), ("summary", self.summary)):
            if '"' in text:
                raise ValueError(f"{label} must not hold a double quote (it would break the frontmatter)")
            if "\n" in text:
                raise ValueError(f"{label} must be one line")
            if EM_DASH in text:
                raise ValueError(f"{label} must not hold the U+2014 character")
        if len(self.summary) > SUMMARY_MAX:
            raise ValueError(f"summary must be at most {SUMMARY_MAX} characters (it has {len(self.summary)})")
        if self.max_chars > _MAX_CHARS_CEIL:
            raise ValueError(f"max_chars must be at most {_MAX_CHARS_CEIL} (it is {self.max_chars})")
        if self.kind not in _PAGE_KINDS:
            raise ValueError(f"kind must be one of {_PAGE_KINDS} (got {self.kind!r})")
        if self.audience not in _AUDIENCES:
            raise ValueError(f"audience must be one of {_AUDIENCES} (got {self.audience!r})")


def scoped(toolset_id: str, names: str) -> tuple[str, ...]:
    """The scoped tool ids of ``toolset_id`` for the whitespace-separated ``names``."""
    return tuple(f"{toolset_id}__{name}" for name in names.split())


def all_specs() -> dict[str, PageSpec]:
    """Every spec the wiki may hold, merged from the modules of ``tests/docs/wiki_specs/``.

    The modules are imported lazily here (not at import time) because they import
    :class:`PageSpec` and :func:`scoped` from this module.
    """
    import tests.docs.wiki_specs as wiki_specs

    specs: dict[str, PageSpec] = {}
    for module_info in pkgutil.iter_modules(wiki_specs.__path__):
        module = importlib.import_module(f"{wiki_specs.__name__}.{module_info.name}")
        for spec in getattr(module, "SPECS", ()):
            if spec.path in specs:
                raise AssertionError(f"two spec modules define the path {spec.path!r}")
            specs[spec.path] = spec
    return specs


def expected_tools(spec: PageSpec) -> list[str]:
    """The tools the page must document: the unconditional ones, then the conditional ones that are live."""
    live = {entry.scoped_id for entry in tool_entries()}
    return [*spec.tools, *(t for t in spec.tools_if_present if t in live)]


_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,})([^\n]*)\n(.*?)^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
_SCOPED_NAME_RE = re.compile(r"[a-z_]+__[a-z0-9_]+")


def _objects(value: Any):
    """Every dict in a parsed JSON value: the top one first, then the nested ones (depth first)."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def calls_in_body(body: str) -> list[str]:
    """The scoped tool ids every json code block of ``body`` calls, in block order.

    A nested object carrying ``toolset_id``, ``tool_name`` and an ``arguments``
    object is a ``call_tool`` call and gives ``<toolset_id>__<tool_name>``; an
    object whose ``name`` is a scoped id and whose ``arguments`` is an object
    is a direct call and gives the name. Blocks that are not JSON are skipped.
    """
    calls: list[str] = []
    for match in _FENCE_RE.finditer(body):
        if not match.group(2).strip().lower().startswith("json"):
            continue
        try:
            value: Any = json.loads(match.group(3))
        except ValueError:
            continue
        for obj in _objects(value):
            if (
                isinstance(obj.get("toolset_id"), str)
                and isinstance(obj.get("tool_name"), str)
                and isinstance(obj.get("arguments"), dict)
            ):
                calls.append(f"{obj['toolset_id']}__{obj['tool_name']}")
            elif (
                isinstance(obj.get("name"), str)
                and _SCOPED_NAME_RE.fullmatch(obj["name"]) is not None
                and isinstance(obj.get("arguments"), dict)
            ):
                calls.append(obj["name"])
    return calls


def render_frontmatter(spec: PageSpec) -> str:
    """The exact frontmatter a spec implies, the head every page of its path starts with."""
    lines = [
        "---",
        f'title: "{spec.title}"',
        f'summary: "{spec.summary}"',
    ]
    if spec.audience == "admin":
        lines.append("audience: admin")
    lines.append(f"order: {spec.order}")
    tools = expected_tools(spec)
    if tools:
        lines.append("tools: [" + ", ".join(tools) + "]")
    if spec.related:
        lines.append("related: [" + ", ".join(spec.related) + "]")
    lines.append("---")
    return "\n".join(lines) + "\n"


PLANNED_PATHS = frozenset({
    "index",
    "agents", "agents/what-an-agent-is", "agents/create-and-edit", "agents/prompts-and-parts", "agents/tools-and-grants", "agents/invoke-an-agent", "agents/switch-and-binding", "agents/compaction",
    "sessions", "sessions/what-a-session-is", "sessions/start-a-session", "sessions/observe-a-session", "sessions/control-a-session", "sessions/messaging-other-sessions", "sessions/find-sessions",
    "workspaces", "workspaces/what-a-workspace-is", "workspaces/create-and-delete", "workspaces/files-and-log", "workspaces/templates", "workspaces/providers", "workspaces/in-session-file-tools", "workspaces/agents-md", "workspaces/prime",
    "graphs", "graphs/what-a-graph-is", "graphs/create-and-edit", "graphs/run-a-graph", "graphs/threads",
    "knowledge", "knowledge/collections", "knowledge/documents", "knowledge/search", "knowledge/navigation", "knowledge/the-platform-wiki",
    "triggers", "triggers/triggers", "triggers/subscriptions", "triggers/fire-now", "triggers/waiting-for-events",
    "channels", "channels/providers", "channels/channels", "channels/bindings", "channels/reply-binding",
    "human-in-the-loop", "human-in-the-loop/ask-user", "human-in-the-loop/inform-user", "human-in-the-loop/open-file", "human-in-the-loop/canvas",
    "toolsets", "toolsets/builtin-toolsets", "toolsets/mcp-toolsets", "toolsets/python-toolsets", "toolsets/list-and-describe-tools",
    "providers", "providers/llm", "providers/model-profiles", "providers/embedding", "providers/cross-encoder", "providers/semantic-search", "providers/artifact-storage", "providers/web-search-and-fetch",
    "approvals", "approvals/policies",
    "harnesses", "harnesses/what-a-harness-is", "harnesses/install-and-sync",
    "web", "web/search-and-fetch", "web/http-and-downloads",
    "utilities", "utilities/time-ids-hashes-math", "utilities/sleep-and-watch-files", "utilities/read-office-documents",
    "not-agent-callable",
    "cookbook", "cookbook/create-and-run-a-session", "cookbook/monitor-and-resume-a-parked-session", "cookbook/run-a-graph-and-collect-results", "cookbook/workspace-as-build-env", "cookbook/scheduled-summariser", "cookbook/daily-incident-digest", "cookbook/pr-reviewer-on-cron", "cookbook/event-driven-data-pipeline", "cookbook/slack-question-answerer", "cookbook/telegram-personal-assistant", "cookbook/discord-moderation-helper", "cookbook/approval-gated-deploy-bot", "cookbook/multi-agent-graph-research", "cookbook/discover-a-capability", "cookbook/internal-tool-via-mcp", "cookbook/run-a-task-through-prime",
    "start-here", "start-here/reading-this-wiki", "start-here/calling-tools", "start-here/roles-and-approvals", "start-here/errors", "start-here/the-core-kit",
})
"""The closed set of the paths the wiki may hold: ``index`` and the 99 hand-written paths.

A page for a path outside it is a bug, and a spec whose path is not here fails
``test_planned_paths_are_slugs_and_cover_every_spec``.
"""

RECIPE_ORDER: tuple[tuple[str, str], ...] = (
    ("workspaces__create_workspace_template", "workspaces__create_workspace"),
    ("workspaces__create_workspace", "workspaces__create_workspace_session"),
    ("workspaces__create_workspace", "trigger__create_subscription"),
    ("workspaces__create_workspace", "system__create_channel_binding"),
    ("system__create_agent", "workspaces__create_workspace_session"),
    ("system__create_agent", "trigger__create_subscription"),
    ("system__create_agent", "system__create_channel_binding"),
    ("system__create_graph", "workspaces__create_workspace_session"),
    ("workspaces__create_workspace_session", "workspaces__get_workspace_session"),
    ("workspaces__create_workspace_session", "workspaces__read_workspace_session_messages"),
    ("workspaces__create_workspace_session", "workspaces__steer_workspace_session"),
    ("workspaces__create_workspace_session", "workspaces__resume_workspace_session"),
    ("workspaces__create_workspace_session", "workspaces__interrupt_workspace_session"),
    ("workspaces__create_workspace_session", "workspaces__cancel_workspace_session"),
    ("workspaces__create_workspace_session", "workspaces__find_sessions"),
    ("workspaces__create_workspace_session", "workspaces__message_session"),
    ("trigger__create", "trigger__create_subscription"),
    ("trigger__create", "system__create_channel_binding"),
    ("trigger__create_subscription", "trigger__fire_now"),
    ("system__create_channel_provider", "system__create_channel"),
    ("system__create_channel", "system__create_channel_binding"),
    ("system__create_channel", "system__set_reply_binding"),
    ("system__create_toolset", "system__list_toolset_tools"),
)
"""The order a recipe's calls must hold: for every ``(before, after)`` pair, the first call of
``after`` may not come before the first call of ``before`` (when both are called). A recipe that
starts a session before it made the workspace, or subscribes before it created the trigger, is
unrunnable as written."""


def order_violations(body: str) -> list[tuple[str, str]]:
    """The :data:`RECIPE_ORDER` pairs whose after-tool is called before the before-tool in ``body``."""
    first: dict[str, int] = {}
    for index, tool in enumerate(calls_in_body(body)):
        first.setdefault(tool, index)
    return [
        (before, after)
        for before, after in RECIPE_ORDER
        if before in first and after in first and first[after] < first[before]
    ]


_CREDENTIAL_PATTERNS = (
    r"xox[abprs]-\d{6,}",
    r"xapp-\d-",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"sk-[A-Za-z0-9_-]{20,}",
    r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)
"""The token shapes a page body must not hold: a real token matches its shape, a placeholder
(``xoxb-REPLACE``, ``<slack-bot-token>``) does not."""


def credential_hits(text: str) -> list[str]:
    """The credential-shaped strings in ``text``; an empty list is clean."""
    return [hit for pattern in _CREDENTIAL_PATTERNS for hit in re.findall(pattern, text)]


@dataclass(frozen=True)
class DriftGuard:
    """A page that must name every member of a set the code defines: the code is the source, and the page must keep up.

    ``names`` is called at check time and returns the strings the page body must hold, one per
    member. The names are wrapped in backticks the way a page shows a name, except the pausing
    tools, which the page lists as bare tool ids.
    """

    page_path: str
    label: str
    names: Callable[[], Iterable[str]]


def _agent_field_names() -> Iterable[str]:
    from primer.model.agent import Agent

    return (f"`{name}`" for name in Agent.model_fields)


def _graph_kind_names() -> Iterable[str]:
    from primer.model.graph import Graph

    schema = Graph.model_json_schema()
    nodes = schema["properties"]["nodes"]["items"]["discriminator"]["mapping"]
    edges = schema["properties"]["edges"]["items"]["discriminator"]["mapping"]
    return (f"`{name}`" for name in (*nodes, *edges))


def _trigger_kinds() -> Iterable[str]:
    from primer.model.trigger import Trigger

    return (f"`{name}`" for name in Trigger.model_json_schema()["properties"]["config"]["discriminator"]["mapping"])


def _subscription_kinds() -> Iterable[str]:
    from primer.model.trigger import Subscription

    return (f"`{name}`" for name in Subscription.model_json_schema()["properties"]["config"]["discriminator"]["mapping"])


def _approval_types() -> Iterable[str]:
    from primer.model.tool_approval import ToolApprovalPolicy

    return (f"`{name}`" for name in ToolApprovalPolicy.model_json_schema()["properties"]["approval"]["discriminator"]["mapping"])


def _builtin_toolset_names() -> Iterable[str]:
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS

    return (f"`{name}`" for name in RESERVED_TOOLSET_IDS)


def _pausing_tool_ids() -> Iterable[str]:
    from tests.docs._wiki import tool_entries

    return (entry.scoped_id for entry in tool_entries() if entry.pauses_turn and entry.scoped_id != "system__call_tool")


def _session_states() -> Iterable[str]:
    import typing

    from primer.model.workspace_session import SessionState

    return (f"`{name}`" for name in typing.get_args(SessionState))


DRIFT_GUARDS = (
    DriftGuard("agents/create-and-edit", "agent fields", _agent_field_names),
    DriftGuard("graphs/what-a-graph-is", "graph node and edge kinds", _graph_kind_names),
    DriftGuard("triggers/triggers", "trigger kinds", _trigger_kinds),
    DriftGuard("triggers/subscriptions", "subscription kinds", _subscription_kinds),
    DriftGuard("approvals/policies", "approval types", _approval_types),
    DriftGuard("toolsets/builtin-toolsets", "built-in toolsets", _builtin_toolset_names),
    DriftGuard("triggers/waiting-for-events", "pausing tools", _pausing_tool_ids),
    DriftGuard("sessions/what-a-session-is", "session states", _session_states),
)
"""The code-defined sets a page must name in full, checked at test time against the live code:
a new field, kind or state lands in the code and fails the page test until the page names it."""

STRICT = False
"""While False, a test that needs a page that is not written yet skips. Task 23 sets it True,
when every planned path holds a page and the ``PLANNED_PATHS`` allowance of the link tests goes."""


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        print(render_frontmatter(all_specs()[path]))
