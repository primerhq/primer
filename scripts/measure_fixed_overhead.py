#!/usr/bin/env python3
"""Measure the FIXED part of a model prompt: the system prompt plus the tool schemas.

A compaction trigger compares a prompt's size with a budget, but the history is only part of
that prompt. The system prompt and the tool catalogue go out on every call and nothing in the
history can give them back, so when they are a large share of the trigger, pruning or
summarising history cannot bring the prompt under it (the anti-thrash rule of the prompt-budget
design). This script puts numbers on that share.

What it measures
  * ``workspace``: the workspace system-prompt fragment plus the workspace tool set every
    workspace-session agent gets, built with the real session and
    ``ToolExecutionManager.for_workspace`` in a throwaway local workspace (needs the git CLI).
  * ``--system-prompt-file FILE`` (repeatable): an agent's rendered system prompt, as text.
  * ``--tools-json FILE``: tool schemas to add, a JSON array (or ``{"tools": [...]}``) of either
    primer ``Tool`` objects (``id``, ``description``, ``toolset_id``, ``args_schema``) or MCP
    ``tools/list`` entries (``name``, ``description``, ``inputSchema``), so the output of
    ``POST /v1/mcp`` ``tools/list`` on a deployment can be fed straight in.

Each component is reported three ways: characters, the ``len/4`` heuristic (what today's live
trigger uses for messages and ignores for these parts), and exact tokens under the o200k and
cl100k encodings when the vocabulary is available (set ``TIKTOKEN_CACHE_DIR``; otherwise the
column says so instead of falling back to an estimate).

``--context-length N`` also prints the live trigger for a model of that size and the fixed
part's share of it, with the anti-thrash threshold (a share of 0.5 or more).

Usage:
    python scripts/measure_fixed_overhead.py [--context-length 32768] [--no-workspace]
        [--system-prompt-file F ...] [--tools-json F ...] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.llm._tokenizer.openai import count_tokens_openai_detailed
from primer.model.chat import Message, TextPart, Tool
from primer.model.except_ import TokenCounterUnavailable

# The live trigger's constants (primer.agent.compaction.CompactionStrategy). Kept here as plain
# numbers so this script has no dependency on the strategy; a test pins them to it.
TRIGGER_RATIO = 0.90
RESERVED_OUTPUT_TOKENS = 8192
ANTI_THRASH_SHARE = 0.5

# A model name that maps to each encoding in the OpenAI counter.
_ENCODINGS = {"o200k": "gpt-4o", "cl100k": "gpt-4"}


def live_trigger(context_length: int) -> int:
    """The trigger today's ``CompactionStrategy`` computes for a model of this size."""
    reserved = min(RESERVED_OUTPUT_TOKENS, max(1, context_length // 2))
    budget = max(0, context_length - reserved)
    return int(TRIGGER_RATIO * budget)


def tools_from_json(payload: Any) -> list[Tool]:
    """Primer ``Tool`` objects or MCP ``tools/list`` entries, as a list or ``{"tools": [...]}``."""
    if isinstance(payload, dict):
        payload = payload.get("tools", payload.get("result", {}).get("tools", []))
    out: list[Tool] = []
    for raw in payload:
        if "args_schema" in raw:
            out.append(Tool.model_validate(raw))
        else:
            out.append(Tool(
                id=raw["name"],
                description=raw.get("description", ""),
                toolset_id=raw.get("toolset_id", "external"),
                args_schema=raw.get("inputSchema") or {"type": "object", "properties": {}},
            ))
    return out


def count_component(
    *, messages: Sequence[Message] = (), tools: Sequence[Tool] = (),
) -> dict[str, int | str]:
    """One component's size: characters, the ``len/4`` heuristic, and exact tokens per encoding."""
    chars = sum(len(p.text) for m in messages for p in m.parts if isinstance(p, TextPart))
    chars += sum(len(json.dumps(t.args_schema)) + len(t.description) + len(t.id) for t in tools)
    row: dict[str, int | str] = {
        "chars": chars,
        "heuristic": count_tokens_char_fallback(messages=list(messages), tools=list(tools) or None),
    }
    for name, model in _ENCODINGS.items():
        try:
            row[name] = count_tokens_openai_detailed(
                model=model, messages=list(messages), tools=list(tools) or None,
            ).total
        except TokenCounterUnavailable as exc:
            row[name] = f"n/a ({exc.message})"
    return row


async def workspace_components() -> tuple[list[Message], list[Tool]]:
    """The system fragment and the tool catalogue of a real workspace session."""
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.model.workspace import (
        LocalWorkspaceConfig,
        WorkspaceProvider,
        WorkspaceProviderType,
        WorkspaceTemplate,
    )
    from primer.model.workspace_session import AgentBinding
    from primer.workspace import WorkspaceBackendFactory

    with tempfile.TemporaryDirectory(prefix="primer-fixed-overhead-") as root:
        backend = WorkspaceBackendFactory.create(WorkspaceProvider(
            id="local-1", provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path=str(Path(root) / "wsroot")),
        ))
        await backend.initialize()
        workspace = await backend.create(WorkspaceTemplate(
            id="measure", description="fixed overhead", provider_id="local-1", files=[],
        ))
        session = await workspace.start_session(
            AgentBinding(agent_id="measure", agent_name="Measure", registered_tool_ids=[]),
            instructions="measure",
        )
        try:
            manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
            tools = await manager.list_tools()
            fragment = Message(role="system", parts=[TextPart(text=session.system_prompt_fragment)])
            return [fragment], list(tools)
        finally:
            await session.aclose()
            await backend.aclose()


async def measure(
    *,
    workspace: bool = True,
    system_prompt_files: Sequence[str] = (),
    tools_json_files: Sequence[str] = (),
    context_length: int | None = None,
) -> dict[str, Any]:
    components: dict[str, dict[str, int | str]] = {}
    all_messages: list[Message] = []
    all_tools: list[Tool] = []
    if workspace:
        messages, tools = await workspace_components()
        components["workspace system fragment"] = count_component(messages=messages)
        components[f"workspace tools ({len(tools)})"] = count_component(tools=tools)
        all_messages += messages
        all_tools += tools
    for path in system_prompt_files:
        message = Message(role="system", parts=[TextPart(text=Path(path).read_text())])
        components[f"system prompt: {Path(path).name}"] = count_component(messages=[message])
        all_messages.append(message)
    for path in tools_json_files:
        tools = tools_from_json(json.loads(Path(path).read_text()))
        components[f"tools: {Path(path).name} ({len(tools)})"] = count_component(tools=tools)
        all_tools += tools
    total = count_component(messages=all_messages, tools=all_tools)
    result: dict[str, Any] = {"components": components, "total": total}
    if context_length is not None:
        trigger = live_trigger(context_length)
        shares: dict[str, float | None] = {}
        for key in ("heuristic", *_ENCODINGS):
            value = total.get(key)
            shares[key] = (value / trigger) if isinstance(value, int) and trigger else None
        result["context"] = {
            "context_length": context_length, "trigger": trigger, "share_of_trigger": shares,
            "anti_thrash_share": ANTI_THRASH_SHARE,
            "history_cannot_help": any(
                s is not None and s >= ANTI_THRASH_SHARE for s in shares.values()
            ),
        }
    return result


def render(result: dict[str, Any]) -> str:
    cols = ("chars", "heuristic", *_ENCODINGS)
    lines = [f"{'component':44s} " + " ".join(f"{c:>10s}" for c in cols)]
    rows = [*result["components"].items(), ("TOTAL (fixed part)", result["total"])]
    for name, row in rows:
        lines.append(f"{name[:44]:44s} " + " ".join(f"{str(row[c])[:10]:>10s}" for c in cols))
    ctx = result.get("context")
    if ctx:
        lines.append("")
        lines.append(f"context_length {ctx['context_length']}: live trigger {ctx['trigger']} tokens")
        for key, share in ctx["share_of_trigger"].items():
            lines.append(f"  fixed part / trigger ({key}): " + ("n/a" if share is None else f"{share:.2f}"))
        verdict = "YES" if ctx["history_cannot_help"] else "no"
        lines.append(
            f"  history cannot bring the prompt under the trigger (share >= {ctx['anti_thrash_share']}): {verdict}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-workspace", action="store_true", help="skip the workspace fragment and tools")
    parser.add_argument("--system-prompt-file", action="append", default=[], metavar="FILE")
    parser.add_argument("--tools-json", action="append", default=[], metavar="FILE")
    parser.add_argument("--context-length", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    result = asyncio.run(measure(
        workspace=not args.no_workspace,
        system_prompt_files=args.system_prompt_file,
        tools_json_files=args.tools_json,
        context_length=args.context_length,
    ))
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
