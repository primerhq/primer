"""The argv and the replayed-tail stdin of one claude CLI step (spec 6.2, 6.5).

Two pure builders; the adapter owns everything with a lifetime (the process,
the bridge step, the timeouts):

* :func:`build_argv` -- the stripped flag set, in the spec 6.2 argv order
  (``--disable-slash-commands``: a message that is a known command name would
  otherwise run it locally, never reaching the model).
* :func:`tail_lines` -- the structured tail (the last user message, or the one
  pending tool round) as stream-json lines for the CLI's stdin.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path

from primer.llm.claude_code.bridge import alias_for
from primer.llm.claude_code.history import replay_tool_id
from primer.model.chat import ImagePart, Message, TextPart, ToolCallPart, ToolResultPart

#: The model-visible name prefix the CLI gives bridge tools (MCP server ``p``).
_MCP_PREFIX = "mcp__p__"


def _image_block(part: ImagePart) -> dict:
    if part.data is not None:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.mime_type or "image/png",
                "data": base64.b64encode(part.data).decode("ascii"),
            },
        }
    # No raw bytes (a url/file_id source): the CLI's stdin takes base64 and
    # fetching here would make tail_lines async, so the image renders as the
    # history renderer's placeholder (spec 10, builder's call).
    return {"type": "text", "text": f"[image: {part.mime_type}]" if part.mime_type else "[image]"}


def _user_content(parts: Sequence) -> list[dict]:
    blocks: list[dict] = []
    for part in parts:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append(_image_block(part))
        # other part types in a user tail are dropped: the CLI's stream-json
        # input takes text and image blocks only.
    return blocks or [{"type": "text", "text": ""}]


def tail_lines(tail: Sequence[Message]) -> list[dict]:
    """The structured tail as stream-json lines for the CLI's stdin.

    A user tail becomes one ``user`` line; a pending tool round becomes one
    ``assistant`` line (``tool_use`` blocks under the model-visible
    ``mcp__p__<alias>`` names, ids replayed through :func:`replay_tool_id`)
    followed by one ``user`` line carrying every ``tool_result`` in call order.
    """
    if tail and tail[0].role == "assistant":
        use_blocks: list[dict] = []
        for part in tail[0].parts:
            if isinstance(part, ToolCallPart):
                use_blocks.append({
                    "type": "tool_use",
                    "id": replay_tool_id(part.id),
                    "name": _MCP_PREFIX + alias_for(part.name),
                    "input": part.arguments,
                })
        result_blocks: list[dict] = []
        for message in tail[1:]:
            if message.role != "tool":
                continue
            for part in message.parts:
                if isinstance(part, ToolResultPart):
                    result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": replay_tool_id(part.id),
                        "content": part.output,
                        "is_error": part.error,
                    })
        return [
            {"type": "assistant", "message": {"role": "assistant", "content": use_blocks}},
            {"type": "user", "message": {"role": "user", "content": result_blocks or [{"type": "text", "text": ""}]}},
        ]
    return [{
        "type": "user",
        "message": {
            "role": "user",
            "content": _user_content(tail[0].parts) if tail else [{"type": "text", "text": ""}],
        },
    }]


def build_argv(
    binary: str,
    tmpdir: Path,
    *,
    model: str,
    mcp_url: str | None,
    effort: str | None,
    json_schema: dict | None,
) -> list[str]:
    """The child's argv, in the spec 6.2 order.

    The stripped flag set always (``--disable-slash-commands`` keeps a message
    that is a known command name from running it locally instead of reaching
    the model); then the bridge (when ``mcp_url`` is given: the MCP server
    ``p`` at that URL, allowed by name), then the reasoning level (when
    ``effort`` is set), then the structured-output schema (when ``json_schema``
    is given).
    """
    argv = [
        binary,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--system-prompt-file", str(tmpdir / "system.md"),
        "--model", model,
    ]
    if mcp_url is not None:
        argv += [
            "--mcp-config",
            json.dumps({"mcpServers": {"p": {"type": "http", "url": mcp_url}}}),
            "--allowedTools", "mcp__p",
        ]
    if effort:
        argv += ["--effort", effort, "--thinking-display", "summarized"]
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema)]
    return argv
