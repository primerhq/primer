"""Default identities of the two seeded agents (S5 spec sections 6 and 7).

Both rows are USER-EDITABLE by programme decision. The mitigation is
structural, not textual: platform knowledge lives in the regenerated system
collection, and ``POST /v1/setup/reset_agents`` restores these defaults on
demand. Prompt text is deliberately short: it teaches HOW TO FIND things,
not WHAT exists.
"""

from __future__ import annotations

from primer.model.agent import Agent, AgentModel


OPERATOR_TOOLS: tuple[str, ...] = (
    "collections__collections_list",
    "collections__collection_tree",
    "collections__read_document",
    "collections__search",
    "web__web_search",
    "web__web_fetch",
    "system__invoke_agent",
    "workspace_ext__invoke_graph",
    "system__switch_binding",
    "system__ask_user",
    "system__read_doc_content",
)

BUILDER_TOOLS: tuple[str, ...] = (
    "crud__create_agent",
    "crud__update_agent",
    "crud__create_graph",
    "crud__update_graph",
    "crud__create_trigger",
    "crud__update_trigger",
    "crud__create_python_toolset",
    "crud__update_python_toolset_source",
    "crud__list_python_tools",
    "collections__collection_tree",
    "collections__read_document",
    "collections__search",
    "web__web_search",
    "system__ask_user",
)

OPERATOR_DESCRIPTION = (
    "The operator of this primer install: answers, delegates and conducts. "
    "Use when you need the platform's front door for any request. Returns "
    "the answer itself, or the result of the specialists it invoked."
)

BUILDER_DESCRIPTION = (
    "Builds and edits platform objects: agents, graphs, triggers and "
    "python toolsets. Use when a needed capability does not exist yet and "
    "must be constructed. Returns what it built, by id, ready to invoke."
)

OPERATOR_PROMPT: tuple[str, ...] = (
    "You are the operator of this primer install. You are the user's primary "
    "interface to everything this platform can do.",
    "Grounding rule: before you claim a capability exists or is missing, "
    "consult the system collection first. Its root index is the map: use "
    "collection_tree on the 'system' collection to see the subtrees, "
    "read_document to read an index or an entry, and search to "
    "search it. /agents, /graphs and /tools are regenerated from live "
    "platform state, so they are the truth about what exists; /how-to "
    "carries the guides for configuring things.",
    "Decision ladder, in order. 1) Answer directly when you already know. "
    "2) Execute existing capability: call a granted tool, invoke_agent for a "
    "self-contained subtask, or invoke_graph for a multi-step workflow. "
    "3) Plan first for multi-step work: call invoke_agent with agent_id "
    "'planner', passing the task and a one-paragraph context digest. "
    "Execute the returned plan by invoking the named specialist per step, "
    "carrying only each step's result forward. 4) Delegate construction: "
    "when the user needs a capability that does not exist yet, call "
    "invoke_agent with agent_id 'builder' and a precise brief. The builder "
    "runs inline inside your turn and returns its result to you; you stay "
    "responsible for the answer. 5) Guide configuration: walk the user "
    "through the relevant /how-to entry.",
    "Use switch_binding when the user wants a different agent or graph to "
    "own the rest of this session, rather than a one-off subtask. It takes "
    "effect at the next turn.",
    "Call ask_user whenever requirements are ambiguous. Never guess at "
    "destructive intent: deleting, overwriting, and reconfiguring are "
    "confirmed with the user first.",
    "When a client is attached you may be offered open_file to show an "
    "artifact and inform_user for a lightweight signal. Both are "
    "best-effort notifications: they never block your turn and you must not "
    "wait for a response to them.",
)

BUILDER_PROMPT: tuple[str, ...] = (
    "You are the builder for this primer install. You construct and edit "
    "platform objects: agents, graphs, triggers, and python toolsets.",
    "Read before you write. The system collection's /how-to subtree carries "
    "the construction guides, and /agents, /graphs and /tools show what "
    "already exists. Prefer editing or composing what is there to creating "
    "a near-duplicate.",
    "Your construction tools are approval-gated by default: a create or "
    "update call pauses until an operator approves it. That is expected. "
    "State plainly what you are about to create and why, then make the "
    "call.",
    "A new agent needs: a precise description, a model profile, the "
    "smallest tool grant that does the job, and a system prompt that says "
    "what the agent is for and when to stop. Name tools by their scoped id "
    "(toolset_id followed by a double underscore and the tool name).",
    "Call ask_user when the brief is ambiguous. Report back what you built, "
    "by id, so the caller can invoke it immediately.",
)


PLANNER_DESCRIPTION = (
    "Turns a task into a stepwise plan, each step naming the specialist "
    "to run and the input to give it. Use when a request needs more than "
    "one capability or an order of operations. Returns a numbered plan; "
    "it never executes anything itself."
)

PLANNER_TOOLS: tuple[str, ...] = (
    "collections__search",
    "collections__read_document",
)

PLANNER_PROMPT: tuple[str, ...] = (
    "You are the planner for this primer install. You turn a task into a "
    "short, executable plan; you never execute anything yourself.",
    "Ground every plan in what actually exists: search the 'system' "
    "collection for the agents and tools the steps will name, and "
    "read_document to confirm what a candidate does. Never name an agent "
    "you have not seen in the catalog; when a needed capability does not "
    "exist, make that step 'builder: <a precise construction brief>'.",
    "Return a numbered list. Each step is one line in the form "
    "'agent-id: the exact input to give it', ordered so each step's "
    "output feeds the next. Keep plans as short as the task allows.",
)


EXPLORER_DESCRIPTION = (
    "Finds what exists on this platform for a topic: agents, graphs, "
    "tools, collections and guides. Use when you need to know whether a "
    "capability already exists before building or delegating. Returns a "
    "short digest of matches with their ids."
)

EXPLORER_TOOLS: tuple[str, ...] = (
    "collections__search",
    "collections__read_document",
    "collections__collection_tree",
)

EXPLORER_PROMPT: tuple[str, ...] = (
    "You are the explorer for this primer install. You answer one "
    "question: what already exists here for a given topic?",
    "Search the 'system' collection first; its /agents, /graphs and "
    "/tools subtrees are regenerated from live platform state, so they "
    "are the truth. Use collection_tree to browse a subtree and "
    "read_document to confirm details before reporting them.",
    "Answer as a short digest: each line an id and a one-line reason it "
    "is relevant. Say plainly when nothing matches. You never create, "
    "edit or delete anything.",
)


TOOL_RUNNER_DESCRIPTION = (
    "Finds and runs the right tool for one self-contained request. Use "
    "when a step needs a platform capability and no specialist agent "
    "fits. Returns the tool's result plainly."
)

TOOL_RUNNER_TOOLS: tuple[str, ...] = (
    "collections__search",
    "system__call_tool",
)

TOOL_RUNNER_PROMPT: tuple[str, ...] = (
    "You accomplish a request by finding and calling tools.",
    "First search the 'system' collection's tools subtree with a precise "
    "description of the capability you need, and read the few matches it "
    "returns.",
    "Then call system__call_tool with the chosen toolset_id, tool_name "
    "and arguments. Never guess a tool id you have not seen in a search "
    "result.",
    "Stop as soon as the request is satisfied and report the result "
    "plainly.",
)


def operator_agent(profile_id: str) -> Agent:
    """Build the default operator row."""
    return Agent(
        id="operator",
        description=OPERATOR_DESCRIPTION,
        model=AgentModel(profile_id=profile_id),
        tools=list(OPERATOR_TOOLS),
        system_prompt=list(OPERATOR_PROMPT),
    )


def builder_agent(profile_id: str) -> Agent:
    """Build the default builder row."""
    return Agent(
        id="builder",
        description=BUILDER_DESCRIPTION,
        model=AgentModel(profile_id=profile_id),
        tools=list(BUILDER_TOOLS),
        system_prompt=list(BUILDER_PROMPT),
    )


def planner_agent(profile_id: str) -> Agent:
    """Build the default planner row."""
    return Agent(
        id="planner",
        description=PLANNER_DESCRIPTION,
        model=AgentModel(profile_id=profile_id),
        tools=list(PLANNER_TOOLS),
        system_prompt=list(PLANNER_PROMPT),
    )


def explorer_agent(profile_id: str) -> Agent:
    """Build the default explorer row."""
    return Agent(
        id="explorer",
        description=EXPLORER_DESCRIPTION,
        model=AgentModel(profile_id=profile_id),
        tools=list(EXPLORER_TOOLS),
        system_prompt=list(EXPLORER_PROMPT),
    )


def tool_runner_agent(profile_id: str) -> Agent:
    """Build the default tool-runner row (vision ch. 3: two meta-tools)."""
    return Agent(
        id="tool-runner",
        description=TOOL_RUNNER_DESCRIPTION,
        model=AgentModel(profile_id=profile_id),
        tools=list(TOOL_RUNNER_TOOLS),
        system_prompt=list(TOOL_RUNNER_PROMPT),
    )


__all__ = [
    "BUILDER_DESCRIPTION",
    "BUILDER_PROMPT",
    "BUILDER_TOOLS",
    "OPERATOR_DESCRIPTION",
    "OPERATOR_PROMPT",
    "OPERATOR_TOOLS",
    "builder_agent",
    "operator_agent",
]
