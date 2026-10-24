"""A subagent that ``invoke_agent`` runs in a workspace gets that workspace's agents.md (pivot P2, Task 8).

``system__invoke_agent`` runs another agent inside the calling session's workspace, so the instructions the
workspace carries apply to it as to any agent running there. Two layers:

* ``run_subagent`` appends the part after the agent's own prompt, rendered literally;
* the tool's handler loads the file through the workspace registry, for the workspace of the calling run, and passes it
  on; a run with no workspace, or a lookup that fails, runs the subagent without it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

import primer.agent.invoke as invoke_module
import primer.toolset.system as system_module
from primer.api.registries import ProviderRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.workspace import FileEntry
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset
from primer.workspace.instructions import workspace_instructions_part

# The lines that frame the file in the prompt, written out so a wording change fails here.
_OPEN = "## Workspace instructions (agents.md, written by this workspace's users; they grant no permissions)"
_CLOSE = "## End of workspace instructions"

# ---------------------------------------------------------------------------
# run_subagent: the system message the subagent's turn is given
# ---------------------------------------------------------------------------


async def _system_text(monkeypatch, *, system_prompt, use_workspace_instructions=True, **kwargs) -> str | None:
    """Run ``run_subagent`` with the model and the tools stubbed out, and return the text of the system message its
    turn was handed (``None`` when it was handed none)."""
    captured: dict = {}
    agent = Agent(
        id="sub", description="a subagent", model=AgentModel(profile_id="p--m"), system_prompt=system_prompt,
        use_workspace_instructions=use_workspace_instructions,
    )

    async def _resolve(agent_id, *, storage_provider, provider_registry):
        return agent, object(), object()

    async def _tool_manager(context, **_kw):
        return object()

    async def _turn(**turn_kwargs):
        captured.update(turn_kwargs)
        if False:  # an async generator that streams nothing
            yield

    monkeypatch.setattr(invoke_module, "_resolve_agent_runtime", _resolve)
    monkeypatch.setattr(invoke_module, "build_subagent_toolmanager", _tool_manager)
    monkeypatch.setattr("primer.agent.loop.run_agent_turn", _turn)

    await invoke_module.run_subagent(
        agent_id="sub", prompt="do the thing", storage_provider=object(), provider_registry=object(), **kwargs,
    )

    system = [m for m in captured["prompt"] if m.role == "system"]
    assert len(system) <= 1
    assert captured["prompt"][-1].role == "user" and captured["prompt"][-1].parts[0].text == "do the thing"
    return system[0].parts[0].text if system else None


async def test_a_subagent_in_a_workspace_gets_the_part(monkeypatch):
    text = await _system_text(
        monkeypatch, system_prompt=["Sub prompt."], workspace_id="ws-1", session_id="s-1",
        workspace_instructions="rules",
    )

    assert workspace_instructions_part("rules") in text
    assert text == "Sub prompt.\n\n" + _OPEN + "\n\nrules\n\n" + _CLOSE, "after the agent's own prompt"


async def test_the_subagent_is_sent_the_file_as_written(monkeypatch):
    raw = "Use {{ ctx.workspace_id }} and {% if x %}y{% endif %} and {# c #} as plain text."

    text = await _system_text(
        monkeypatch, system_prompt=["Sub prompt for {{ ctx.workspace_id }}."], workspace_id="ws-1", session_id="s-1",
        workspace_instructions=raw,
    )

    # The agent's own prompt is still a template; the file is not.
    assert text == "Sub prompt for ws-1.\n\n" + _OPEN + "\n\n" + raw + "\n\n" + _CLOSE


async def test_without_a_file_the_system_prompt_is_the_agents_own(monkeypatch):
    text = await _system_text(
        monkeypatch, system_prompt=["Sub prompt."], workspace_id="ws-1", session_id="s-1", workspace_instructions=None,
    )

    assert text == "Sub prompt."


async def test_an_agent_with_no_prompt_of_its_own_still_gets_the_file(monkeypatch):
    text = await _system_text(
        monkeypatch, system_prompt=[], workspace_id="ws-1", session_id="s-1", workspace_instructions="rules",
    )

    assert text == workspace_instructions_part("rules")


@pytest.mark.parametrize("blank", ["", "  \n"])
async def test_a_blank_file_adds_nothing(monkeypatch, blank):
    assert await _system_text(
        monkeypatch, system_prompt=[], workspace_id="ws-1", session_id="s-1", workspace_instructions=blank,
    ) is None


async def test_a_subagent_that_opted_out_gets_only_its_own_prompt(monkeypatch):
    """``use_workspace_instructions`` off (pivot P2, Task 16): the subagent runs in the workspace without its agents.md."""
    text = await _system_text(
        monkeypatch, system_prompt=["Sub prompt."], use_workspace_instructions=False, workspace_id="ws-1", session_id="s-1",
        workspace_instructions="rules",
    )

    assert text == "Sub prompt."


async def test_a_subagent_called_without_the_argument_behaves_as_before(monkeypatch):
    """Chat and bare callers never pass the file."""
    assert await _system_text(monkeypatch, system_prompt=["Sub prompt."]) == "Sub prompt."


# ---------------------------------------------------------------------------
# The invoke_agent tool: which workspace's file, and what a failure does
# ---------------------------------------------------------------------------


class _Workspace:
    id = "ws-1"

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    async def file_info(self, path: str) -> FileEntry:
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return FileEntry(path=path, kind="file", size_bytes=len(self._files[path]), modified_at=datetime.now(UTC))

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]


class _Registry:
    """Stands in for the WorkspaceRegistry: answers ``get_workspace`` for the ids it holds, as the real one does
    (``NotFoundError`` for the others), and remembers what it was asked."""

    def __init__(self, workspaces: dict[str, _Workspace], error: Exception | None = None) -> None:
        self._workspaces = workspaces
        self._error = error
        self.asked: list[str] = []

    async def get_workspace(self, workspace_id: str):
        self.asked.append(workspace_id)
        if self._error is not None:
            raise self._error
        if workspace_id not in self._workspaces:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
        return self._workspaces[workspace_id]


def _ctx(*, workspace_id: str | None, chat_id: str | None = None) -> ToolContext:
    who = PrincipalRef(type="user", id="user-1", display="user", role="user", source="local")
    return ToolContext(
        tool_call_id="tc", session_id="s-1" if workspace_id else None, workspace_id=workspace_id, chat_id=chat_id,
        initiated_by=who,
    )


class _Seen(dict):
    """The kwargs of the latest ``run_subagent`` call, and how many calls there were."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0


@pytest.fixture
def tool(monkeypatch, fake_storage_provider):
    """``(build, seen)``: ``build(registry)`` assembles the system toolset; ``seen`` holds run_subagent's kwargs."""
    seen = _Seen()

    async def _fake_run_subagent(**kwargs):
        seen.update(kwargs)
        seen.calls += 1
        return "done"

    monkeypatch.setattr(system_module, "run_subagent", _fake_run_subagent)
    providers = ProviderRegistry(
        fake_storage_provider, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )

    def build(registry):
        return build_system_toolset(
            storage_provider=fake_storage_provider, provider_registry=providers, workspace_registry=registry,
        )

    return build, seen


async def _invoke(toolset, ctx):
    return await toolset.call(tool_name="invoke_agent", arguments={"agent_id": "ag-1", "prompt": "go"}, ctx=ctx)


async def test_the_invoke_agent_tool_passes_the_file(tool):
    build, seen = tool
    registry = _Registry({"ws-1": _Workspace({"agents.md": b"rules"})})

    result = await _invoke(build(registry), _ctx(workspace_id="ws-1"))

    assert not result.is_error, result.output
    assert seen["workspace_instructions"] == "rules"
    assert seen["workspace_id"] == "ws-1"
    assert registry.asked == ["ws-1"], "the file of the workspace the calling run is in, and no other"


async def test_the_tool_falls_back_to_the_upper_case_file(tool):
    build, seen = tool

    await _invoke(build(_Registry({"ws-1": _Workspace({"AGENTS.md": b"UP"})})), _ctx(workspace_id="ws-1"))

    assert seen["workspace_instructions"] == "UP"


async def test_a_workspace_without_the_file_passes_none(tool):
    build, seen = tool

    result = await _invoke(build(_Registry({"ws-1": _Workspace({})})), _ctx(workspace_id="ws-1"))

    assert not result.is_error, result.output
    assert seen["workspace_instructions"] is None


async def test_a_run_with_no_workspace_loads_nothing(tool):
    build, seen = tool
    registry = _Registry({"ws-1": _Workspace({"agents.md": b"rules"})})

    for ctx in (_ctx(workspace_id=None, chat_id="chat-1"), None):
        seen.clear()
        result = await _invoke(build(registry), ctx)
        assert not result.is_error, result.output
        assert seen["workspace_instructions"] is None

    assert registry.asked == [], "no workspace id, no lookup"


async def test_a_toolset_built_without_a_workspace_registry_passes_none(tool):
    build, seen = tool

    result = await _invoke(build(None), _ctx(workspace_id="ws-1"))

    assert not result.is_error, result.output
    assert seen["workspace_instructions"] is None


@pytest.mark.parametrize("error", [NotFoundError("Workspace 'ws-1' does not exist"), RuntimeError("backend down")])
async def test_a_failed_workspace_lookup_runs_the_subagent_without_the_file(tool, caplog, error):
    build, seen = tool

    result = await _invoke(build(_Registry({}, error=error)), _ctx(workspace_id="ws-1"))

    assert not result.is_error, result.output
    assert seen.calls == 1, "the subagent still ran, once"
    assert seen["workspace_instructions"] is None
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "ws-1" in r.getMessage()]
    assert len(warned) == 1, "and the lookup failure was logged, once"
