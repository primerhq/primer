"""Agent definition model.

An agent is the user-facing unit of "an LLM with a personality and a
toolset." Stored as a :class:`Document` in an agents
:class:`matrix.model.collection.Collection` (the ``description``
inherited from :class:`Describeable` doubles as embedding-text for
vector search over agents in a future release).

Carries:

* identity + description (from :class:`Describeable`).
* :attr:`Agent.model` -- which configured
  :class:`matrix.model.provider.LLMProvider` + which model name.
* :attr:`Agent.temperature` -- sampling temperature for every call.
* :attr:`Agent.tools` -- first-class tools registered with the agent
  (referenced by id; resolved against the application's toolsets at
  session start).
* :attr:`Agent.system_prompt` -- multi-part system prompt; segments
  are joined by the runtime when building the LLM call (matches
  Anthropic's multi-part ``system`` shape; concatenated with
  ``\\n\\n`` for OpenAI / Ollama).

This is the *definition* model. The per-session snapshot taken at
session start is :class:`matrix.model.session.AgentBinding` -- two
distinct types because session history is preserved even after the
underlying agent definition has been edited or deleted.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from primer.model.common import Describeable


class AgentModel(BaseModel):
    """Reference to the LLM the agent talks to.

    Field name is ``model`` on :class:`Agent` so the type name carries
    a tiny collision with the Pydantic-internal sense of "model" --
    the docstring + module context disambiguate.
    """

    provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the configured "
            ":class:`matrix.model.provider.LLMProvider` this agent uses. "
            "Resolved against the application's provider registry at "
            "session-start time; not validated at construction."
        ),
    )
    model_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side model name (e.g. 'gpt-4o-mini', "
            "'claude-sonnet-4-6', 'gemini-2.5-flash'). Must be one of "
            "the models the referenced provider permits, but that "
            "constraint is checked by the runtime, not here."
        ),
    )


class Agent(Describeable):
    """A configured agent definition.

    Inherits ``id`` and ``description`` from :class:`Describeable`.
    The ``description`` is intended for human display AND for vector
    indexing once an agents :class:`matrix.model.collection.Collection`
    is introduced.
    """

    model: AgentModel = Field(
        ...,
        description="Which LLM provider + model the agent talks to.",
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Sampling temperature. ``None`` defers to the LLM "
            "adapter's default. Lower bound is 0.0; no upper bound "
            "is enforced because providers disagree (OpenAI / Ollama "
            "accept up to ~2.0, Anthropic / Google cap at 1.0). The "
            "adapter is responsible for clamping or rejecting values "
            "outside its own range."
        ),
    )
    tools: list[str] = Field(
        default_factory=list,
        description=(
            "Scoped tool ids the agent has access to, each of the form "
            "``<toolset_id>__<tool_name>`` (e.g. ``system__list_files``). "
            "Empty list means the agent has NO tools registered. The "
            "runtime derives the set of toolset providers to resolve "
            "from the unique prefixes of this list, and exposes exactly "
            "the listed tools to the LLM — never a whole toolset. "
            "Workspace tools are NOT listed here; they are composed "
            "onto the agent automatically when it attaches to a "
            "workspace (see :class:`matrix.workspace.session.AgentSession`)."
        ),
    )
    system_prompt: list[str] = Field(
        default_factory=list,
        description=(
            "Multi-part system prompt. Segments are joined by the "
            "runtime when building the LLM call -- preserving the "
            "list shape lets adapters that natively support a "
            "multi-segment system parameter (Anthropic) emit it "
            "directly, while adapters that take a single string "
            "(OpenAI / Ollama) join with ``\\n\\n``. An empty list "
            "means the agent has no system prompt."
        ),
    )
    compaction_prompt: list[str] = Field(
        default_factory=list,
        description=(
            "Instructions the runtime uses to compact the agent's "
            "conversation when it exceeds the configured LLM's context "
            "window. Multi-part for the same reason as "
            ":attr:`system_prompt` (segments joined by the runtime). "
            "Compaction strategy is agent-specific because what to keep "
            "and what to drop depends on the agent's purpose -- a "
            "researcher might preserve cited sources, a coder might "
            "preserve the current file under edit. An empty list means "
            "the runtime falls back to its default compaction prompt."
        ),
    )
    harness_id: str | None = Field(
        default=None,
        description=(
            "When set, this row is managed by the named harness. "
            "Mutation through the public CRUD endpoints returns 409 — "
            "use the harness's sync/uninstall flow instead."
        ),
    )


__all__ = [
    "Agent",
    "AgentModel",
]
