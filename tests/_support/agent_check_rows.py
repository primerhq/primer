"""Row builders for the agent create/update existence checks (finding A-09, the create/update half), shared by the REST, system-tool and
builder-toolset suites so all three seed and submit exactly the same things."""

from __future__ import annotations

from primer.model.agent import Agent, AgentModel
from primer.model.model_profile import ModelProfile
from primer.model.provider import McpConfig, StdioConfig, Toolset, ToolsetProviderType, TransportType


def profile_row(profile_id: str) -> ModelProfile:
    """A stored single ModelProfile. The agent check looks the profile up by id; it does not look at the provider behind it."""
    return ModelProfile(
        id=profile_id, description="a profile", kind="single", provider_id="llm-ref", model_name="m", context_length=1000,
    )


def toolset_row(toolset_id: str) -> Toolset:
    """A stored MCP toolset row (stdio), enough to exist as a toolset an agent's tools can name."""
    return Toolset(
        id=toolset_id,
        provider=ToolsetProviderType.MCP,
        config=McpConfig(transport=TransportType.STDIO, config=StdioConfig(command=["echo"])),
    )


def agent_body(agent_id: str, *, profile_id: str = "mp-1", tools: list[str] | None = None) -> dict:
    return Agent(
        id=agent_id, description="a test agent", model=AgentModel(profile_id=profile_id), tools=list(tools or []),
        system_prompt=["x"],
    ).model_dump(mode="json")


# The words of the two refusals, defined ONCE so the REST suite and the tool suites expect the same sentence (the tool prefixes the field).
def profile_missing_message(profile_id: str) -> str:
    return f"ModelProfile {profile_id!r} does not exist; create the profile first or name an existing one"


def toolsets_missing_message(*toolset_ids: str) -> str:
    names = ", ".join(repr(t) for t in sorted(set(toolset_ids)))
    return f"tools name toolsets that do not exist: {names}; create them first or remove those tools"
