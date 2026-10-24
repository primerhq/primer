---
title: "Python toolsets"
summary: "Turn a Python module into a toolset: how functions become tools, and how to create and update one."
order: 3
tools: [crud__create_python_toolset, crud__update_python_toolset_source, crud__list_python_tools]
related: [toolsets/mcp-toolsets, approvals/policies]
---
## What it is
A Python toolset is a stored `Toolset` row with `provider: "python"`: one module whose decorated functions become tools. Its source runs on the server host, so creating or changing one needs an admin.

## Concepts
- Every top-level function decorated with `@primer_tool()` becomes a tool named after the function, scoped `<toolset id>__<function>` (`invoice-tools__vat_total`). `primer_tool`, `resumes`, `ask_user`, `sleep_for` and `watch_files` are already defined in the module: do not import them.
- Registration reads the source without running it, and refuses the first bad function by name, field and line. Each tool needs:
  - a docstring with a summary line, a `Use when ...` line (or a `When:` section) and an `Args:` entry for every argument;
  - a type annotation on every argument: `str`, `int`, `float`, `bool`, `list[...]`, `dict`, or a union with `None`. No `*args` or `**kwargs`.
- The signature is the schema. An argument with a default is optional, and an argument the signature does not name fails the call. An optional `Examples:` section holds one JSON object of arguments per line, and each must match the schema.
- A parameter named `ctx` is not an argument: it receives the call's context as plain data (`tool_call_id`, `session_id`, `workspace_id`, `parked_at`). There is no `inform`.
- The return value must be JSON-serialisable. A string comes back as it is, anything else as JSON text. An exception comes back as an error result with its type and message.
- A yielding tool pauses the turn. It calls `ask_user(question)`, `sleep_for(seconds)` or `watch_files(paths)`, each with an optional `meta`, and has a `@resumes(<tool>)` companion that receives `payload` and `meta` when the answer arrives:

```python
@primer_tool(timeout_seconds=60)
def ask_the_operator(question: str) -> str:
    """Ask the operator a question and wait for the reply.

    Use when a decision needs a person.

    Args:
        question: What to ask, in one sentence.
    """
    return ask_user(question, meta={"question": question})


@resumes(ask_the_operator)
def _answer(payload: dict, meta: dict) -> str:
    """Return the operator's answer.

    Use when resuming the ask.

    Args:
        payload: The response payload.
        meta: The resume metadata.
    """
    return payload["response"]
```

- Timeouts: `@primer_tool(timeout_seconds=...)` per tool, a literal number above 0 and at most 300. A tool that sets none gets the toolset's `default_timeout_seconds` (30 unless you set it). At the limit the process is killed and the call fails.
- The sandbox: each call is a fresh Python process with the standard library only, never primer's own modules, and with limits on CPU, memory, file size and open files. Outbound network is denied unless the toolset sets `allow_network`, and subprocesses are denied, but only where the host's isolation level enforces them (it is `container`, `seccomp`, `sandbox-exec` or `rlimit-only`, the weakest). Third-party packages exist only on container backends, from the toolset's `image`.
- The three `crud` tools: `crud__create_python_toolset` and `crud__update_python_toolset_source` need an admin, and every `crud` tool is approval-gated by default, `crud__list_python_tools` included. `env`, `allow_network` and `image` are not theirs to set: an admin writes them with `system__update_toolset` and a full `python` body, which parks for an admin's approval (see [MCP toolsets](toolsets/mcp-toolsets)).

## How to
1. Write the module: one decorated function per tool, each with its docstring and annotations.
2. Create the toolset with `crud__create_python_toolset`. Expect the call to park until it is approved.
3. Read the answer: `ok: true` lists the tools; `ok: false` carries the `error`, the `field` and the `lineno` to fix.
4. Grant `invoice-tools__vat_total` in an agent's `tools`, or call it through `system__call_tool` with `invoice-tools` as the `toolset_id` and `vat_total` as the `tool_name`.
5. Change the code with `crud__update_python_toolset_source`. The next call runs the new source.

## Tool calls
### Create a toolset
`crud__create_python_toolset` registers the source and stores the row. Needs an admin.

```json
{"toolset_id": "crud", "tool_name": "create_python_toolset", "arguments": {"toolset_id": "invoice-tools", "source": "@primer_tool()\ndef vat_total(amount: float, rate: float = 0.2) -> dict:\n    \"\"\"Add VAT to a net amount.\n\n    Use when an invoice line needs its gross total.\n\n    Args:\n        amount: The net amount.\n        rate: The VAT rate, as a fraction.\n    \"\"\"\n    return {\"net\": amount, \"gross\": round(amount * (1 + rate), 2)}\n", "default_timeout_seconds": 10}}
```

Reference: [`crud__create_python_toolset`](reference/tools/crud/create_python_toolset)

### Replace the source
`crud__update_python_toolset_source` replaces the whole module and bumps `source_version`. Needs an admin. This one adds an `Examples:` line.

```json
{"toolset_id": "crud", "tool_name": "update_python_toolset_source", "arguments": {"toolset_id": "invoice-tools", "source": "@primer_tool()\ndef vat_total(amount: float, rate: float = 0.2) -> dict:\n    \"\"\"Add VAT to a net amount.\n\n    Use when an invoice line needs its gross total.\n\n    Args:\n        amount: The net amount.\n        rate: The VAT rate, as a fraction.\n\n    Examples:\n        {\"amount\": 100.0, \"rate\": 0.2}\n    \"\"\"\n    return {\"net\": amount, \"gross\": round(amount * (1 + rate), 2)}\n"}}
```

Reference: [`crud__update_python_toolset_source`](reference/tools/crud/update_python_toolset_source)

### List the derived tools
`crud__list_python_tools` derives the tools from the stored source: each `id`, whether it `yields`, its `timeout_seconds` and its argument names. It runs none of them.

```json
{"toolset_id": "crud", "tool_name": "list_python_tools", "arguments": {"toolset_id": "invoice-tools"}}
```

Reference: [`crud__list_python_tools`](reference/tools/crud/list_python_tools)

## Gotchas
- A source that fails registration is not an error result. The call succeeds with `{"ok": false, ...}` and nothing is stored, and an unknown id answers `{"ok": false, "error": "not-found"}`. Read `ok`.
- A run below admin is refused by the two write tools before any approval (`forbidden` through `system__call_tool`), so no approval helps. Ask an admin.
- `crud__list_python_tools` parks for approval like the rest of `crud`. To see the tools without a gate, call `system__list_toolset_tools` with the toolset id.
- Every update bumps `source_version`, even with the same source. A call parked in one of the toolset's yielding tools does not resume on new code: it ends with an error naming both versions, and the answer is not applied.
- Nothing survives in memory across a park: the two halves of a yielding tool are separate processes. Put what the companion needs in `meta`. A yield helper called outside a session, or at module level, is an error.
- A taken id fails the create: update the source instead. A built-in or scope id (`web`, `workspace`) answers `conflict`, and so does editing the source of a toolset a harness manages.
- Give these tools a Python toolset's id only. Given an MCP toolset's id, they fail with a bare internal error.

## Related
[MCP toolsets](toolsets/mcp-toolsets), [approval policies](approvals/policies), [built-in toolsets](toolsets/builtin-toolsets), [list and describe tools](toolsets/list-and-describe-tools).
