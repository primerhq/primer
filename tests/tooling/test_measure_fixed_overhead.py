"""scripts/measure_fixed_overhead.py: the size of the part of a prompt no history can give back.

The script keeps its own copy of the live compaction trigger, and the numbers it prints are
only worth anything if that copy cannot drift from the real one, so the first tests pin it to
``CompactionStrategy``. The rest pin what the report claims: components are counted, an exact
column says plainly when the vocabulary is missing instead of falling back to an estimate, and
the share of the trigger flips the way the anti-thrash rule needs it to.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.llm._tokenizer import _tiktoken_offline
from primer.model.chat import Message, TextPart

ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "measure_fixed_overhead", ROOT / "scripts" / "measure_fixed_overhead.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_script()

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available on PATH (StateRepo needs it)",
)


class TestLiveTriggerCopy:
    @pytest.mark.parametrize("context_length", [1, 4096, 8192, 16384, 16385, 32768, 128_000, 1_000_000])
    def test_it_matches_the_compaction_strategy(self, context_length: int) -> None:
        strategy = CompactionStrategy()
        budget = strategy._effective_budget(SimpleNamespace(context_length=context_length))
        assert mod.live_trigger(context_length) == int(strategy.trigger_ratio * budget)

    def test_the_constants_are_the_strategy_defaults(self) -> None:
        assert mod.TRIGGER_RATIO == CompactionStrategy.DEFAULT_TRIGGER_RATIO
        assert mod.RESERVED_OUTPUT_TOKENS == CompactionStrategy.DEFAULT_RESERVED_OUTPUT


class TestToolsFromJson:
    MCP = {"name": "ls", "description": "list", "inputSchema": {"type": "object", "properties": {"p": {"type": "string"}}}}
    PRIMER = {"id": "x__cat", "description": "read", "toolset_id": "x", "args_schema": {"type": "object", "properties": {}}}

    def test_mcp_entries_become_tools(self) -> None:
        (tool,) = mod.tools_from_json([self.MCP])
        assert (tool.id, tool.description, tool.toolset_id) == ("ls", "list", "external")
        assert tool.args_schema["properties"]["p"] == {"type": "string"}

    def test_primer_tool_objects_are_taken_as_they_are(self) -> None:
        (tool,) = mod.tools_from_json([self.PRIMER])
        assert (tool.id, tool.toolset_id) == ("x__cat", "x")

    def test_a_tools_wrapper_and_a_json_rpc_result_are_both_unwrapped(self) -> None:
        assert [t.id for t in mod.tools_from_json({"tools": [self.MCP]})] == ["ls"]
        assert [t.id for t in mod.tools_from_json({"result": {"tools": [self.MCP, self.PRIMER]}})] == ["ls", "x__cat"]

    def test_an_mcp_entry_without_a_schema_still_has_an_object_schema(self) -> None:
        (tool,) = mod.tools_from_json([{"name": "ping"}])
        assert tool.args_schema == {"type": "object", "properties": {}}


class TestCountComponent:
    def test_it_reports_characters_the_heuristic_and_both_encodings(self) -> None:
        message = Message(role="system", parts=[TextPart(text="x" * 400)])
        row = mod.count_component(messages=[message])
        assert row["chars"] == 400
        assert isinstance(row["heuristic"], int) and row["heuristic"] >= 100
        assert isinstance(row["o200k"], int) and isinstance(row["cl100k"], int)

    def test_tools_are_counted_through_their_schema(self) -> None:
        small = mod.tools_from_json([{"name": "a", "description": "d"}])
        large = mod.tools_from_json([{
            "name": "a", "description": "d" * 2000,
            "inputSchema": {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(40)}},
        }])
        assert mod.count_component(tools=large)["heuristic"] > mod.count_component(tools=small)["heuristic"] * 10

    @pytest.mark.real_tiktoken_loader
    def test_a_missing_vocabulary_is_said_not_estimated(self, monkeypatch, tmp_path) -> None:
        """No silent fallback: a column the vocabulary cannot fill says so."""
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
        _tiktoken_offline.reset()
        try:
            row = mod.count_component(messages=[Message(role="system", parts=[TextPart(text="hello")])])
        finally:
            _tiktoken_offline.reset()
        assert isinstance(row["heuristic"], int), "the heuristic column never needs a vocabulary"
        assert str(row["o200k"]).startswith("n/a (") and str(row["cl100k"]).startswith("n/a (")


class TestMeasure:
    async def test_extra_inputs_are_added_to_the_fixed_part(self, tmp_path) -> None:
        prompt = tmp_path / "agent.txt"
        prompt.write_text("You are a careful agent. " * 40)
        tools = tmp_path / "tools.json"
        tools.write_text(json.dumps([{"name": "search", "description": "find things", "inputSchema": {"type": "object"}}]))
        result = await mod.measure(
            workspace=False, system_prompt_files=[str(prompt)], tools_json_files=[str(tools)], context_length=32768,
        )
        assert list(result["components"]) == ["system prompt: agent.txt", "tools: tools.json (1)"]
        parts = [row["heuristic"] for row in result["components"].values()]
        # the heuristic is additive across messages and tools, so the fixed part is exactly their sum
        assert result["total"]["heuristic"] == sum(parts)
        assert result["context"]["trigger"] == mod.live_trigger(32768)

    @needs_git
    async def test_the_workspace_fixed_part_is_measured_from_a_real_session(self) -> None:
        result = await mod.measure(workspace=True)
        names = list(result["components"])
        assert names[0] == "workspace system fragment"
        assert names[1].startswith("workspace tools (") and not names[1].startswith("workspace tools (0)")
        assert result["total"]["heuristic"] > result["components"][names[0]]["heuristic"]

    @needs_git
    async def test_the_share_of_the_trigger_flips_with_the_context_length(self) -> None:
        """A small context is dominated by the fixed part (history cannot help); a large one is not."""
        small = (await mod.measure(workspace=True, context_length=4096))["context"]
        large = (await mod.measure(workspace=True, context_length=200_000))["context"]
        assert small["share_of_trigger"]["heuristic"] > mod.ANTI_THRASH_SHARE and small["history_cannot_help"]
        assert large["share_of_trigger"]["heuristic"] < mod.ANTI_THRASH_SHARE and not large["history_cannot_help"]


class TestCli:
    def test_json_output_is_machine_readable(self, tmp_path, capsys) -> None:
        prompt = tmp_path / "p.txt"
        prompt.write_text("hello world")
        assert mod.main(["--no-workspace", "--system-prompt-file", str(prompt), "--context-length", "8192", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["context"]["context_length"] == 8192
        assert "system prompt: p.txt" in payload["components"]

    def test_the_table_has_a_total_row_and_a_verdict(self, tmp_path, capsys) -> None:
        prompt = tmp_path / "p.txt"
        prompt.write_text("hello world")
        assert mod.main(["--no-workspace", "--system-prompt-file", str(prompt), "--context-length", "8192"]) == 0
        out = capsys.readouterr().out
        assert "TOTAL (fixed part)" in out
        assert "history cannot bring the prompt under the trigger" in out
